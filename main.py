from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


TEXT_TO_IMAGE_KEYWORDS = [
    "生成图片",
    "画一张",
    "帮我画",
    "出图",
    "生图",
    "生成一张",
    "画个",
    "画一个",
    "draw",
    "generate image",
]

IMAGE_TO_IMAGE_KEYWORDS = [
    "图生图",
    "改图",
    "参考这张图",
    "基于这张图",
    "把这张图",
    "换成",
    "改成",
    "转成",
    "风格化",
    "image to image",
]

CONTEXT_IMAGE_REFERENCE_KEYWORDS = [
    "上面那张图",
    "上面的图",
    "上一张图",
    "刚才那张图",
    "刚刚那张图",
    "这张图",
    "这个图",
    "那张图",
    "把它",
    "基于它",
    "参考它",
    "previous image",
    "above image",
    "that image",
]

CONTEXT_IMAGE_MODIFICATION_KEYWORDS = [
    "改成",
    "换成",
    "转成",
    "风格化",
    "修改",
    "重画",
    "变成",
    "润色",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "api_base_url": "",
    "api_key": "",
    "text_to_image_path": "/v1/images/generations",
    "image_to_image_path": "/v1/images/edits",
    "model": "image-model",
    "default_image_size": "1024x1024",
    "timeout_seconds": 60,
    "auto_recognize": True,
    "trigger_keywords": [],
    "image_to_image_trigger_keywords": [],
    "enable_context_image_reference": True,
    "recent_image_ttl_seconds": 600,
    "max_cached_images_per_session": 1,
    "max_input_image_bytes": 10 * 1024 * 1024,
    "max_output_image_bytes": 20 * 1024 * 1024,
    "max_concurrent_generations": 2,
    "api_adapter_mode": "generic",
    "negative_prompt": "",
    "quality": "",
    "style": "",
    "seed": "",
    "steps": 0,
    "guidance_scale": 0.0,
    "output_format": "",
    "request_extra_json": {},
    "request_extra_form_fields": {},
    "custom_headers": {},
    "auth_header_name": "Authorization",
    "auth_scheme": "Bearer",
    "retry_attempts": 2,
    "retry_backoff_seconds": 1.0,
    "retry_status_codes": [429, 500, 502, 503, 504],
    "session_cooldown_seconds": 0,
    "failure_circuit_breaker_threshold": 3,
    "failure_circuit_breaker_seconds": 60,
    "fallback_text_to_image_on_img2img_failure": False,
    "generation_progress_message": "正在生成图片，请稍等……",
    "generation_failed_message": "图片生成失败，请稍后再试。",
    "missing_image_message": "请先发送图片，或将图片和修改要求放在同一条消息中。",
    "input_too_large_message": "图片过大，请压缩后再试。",
    "cooldown_message": "请求过于频繁，请稍后再试。",
    "service_unavailable_message": "生图服务暂时不可用，请稍后再试。",
    "fallback_notice_message": "图生图失败，已尝试按文本描述生成。",
}


class ImageGenerationError(RuntimeError):
    """Raised when image generation or result parsing fails."""


class ImageGenerationHTTPError(ImageGenerationError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"API 返回错误，HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(slots=True)
class ImageGenerationClientConfig:
    api_base_url: str
    api_key: str
    text_to_image_path: str
    image_to_image_path: str
    model: str
    default_image_size: str
    timeout_seconds: int
    max_output_image_bytes: int
    api_adapter_mode: str
    negative_prompt: str
    quality: str
    style: str
    seed: str
    steps: int
    guidance_scale: float
    output_format: str
    request_extra_json: dict[str, Any]
    request_extra_form_fields: dict[str, Any]
    custom_headers: dict[str, str]
    auth_header_name: str
    auth_scheme: str
    retry_attempts: int
    retry_backoff_seconds: float
    retry_status_codes: list[int]


@dataclass(slots=True)
class CachedImage:
    image_bytes: bytes
    created_at: float
    message_id: str
    size: int


class RecentImageStore:
    def __init__(self, ttl_seconds: int, max_images_per_session: int, max_image_bytes: int):
        self.ttl_seconds = max(1, ttl_seconds)
        self.max_images_per_session = max(1, max_images_per_session)
        self.max_image_bytes = max(1, max_image_bytes)
        self._images: dict[str, list[CachedImage]] = {}

    def put(self, session_key: str, image_bytes: bytes, message_id: str = "") -> bool:
        self.prune()
        if not session_key or len(image_bytes) > self.max_image_bytes:
            return False

        item = CachedImage(
            image_bytes=image_bytes,
            created_at=time.monotonic(),
            message_id=message_id,
            size=len(image_bytes),
        )
        bucket = [item] + self._images.get(session_key, [])
        self._images[session_key] = bucket[: self.max_images_per_session]
        return True

    def get(self, session_key: str) -> CachedImage | None:
        self.prune()
        bucket = self._images.get(session_key)
        return bucket[0] if bucket else None

    def clear(self, session_key: str) -> bool:
        return self._images.pop(session_key, None) is not None

    def prune(self) -> None:
        now = time.monotonic()
        expired_keys: list[str] = []
        for session_key, bucket in self._images.items():
            live = [item for item in bucket if now - item.created_at <= self.ttl_seconds]
            if live:
                self._images[session_key] = live[: self.max_images_per_session]
            else:
                expired_keys.append(session_key)
        for session_key in expired_keys:
            self._images.pop(session_key, None)


class ImageGenerationClient:
    def __init__(self, config: ImageGenerationClientConfig):
        self.config = config

    async def text_to_image(self, prompt: str, image_size: str | None = None) -> bytes | str:
        payload = self._base_payload(prompt, image_size)
        payload.update(self.config.request_extra_json)
        if self.config.api_adapter_mode == "openai_like":
            payload["n"] = 1
        return await self._post_json(self.config.text_to_image_path, payload)

    async def image_to_image(self, prompt: str, image_bytes: bytes, image_size: str | None = None) -> bytes | str:
        return await self._post_form(self.config.image_to_image_path, prompt, image_bytes, image_size)

    def _base_payload(self, prompt: str, image_size: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "prompt": prompt,
            "size": image_size or self.config.default_image_size,
        }
        optional_values: dict[str, Any] = {
            "negative_prompt": self.config.negative_prompt,
            "quality": self.config.quality,
            "style": self.config.style,
            "seed": self.config.seed,
            "steps": self.config.steps,
            "guidance_scale": self.config.guidance_scale,
            "response_format": self.config.output_format,
        }
        for key, value in optional_values.items():
            if value not in ("", 0, 0.0, None):
                payload[key] = value
        return payload

    def _build_form(self, prompt: str, image_bytes: bytes, image_size: str | None = None) -> aiohttp.FormData:
        form = aiohttp.FormData()
        for key, value in self._base_payload(prompt, image_size).items():
            form.add_field(key, str(value))
        for key, value in self.config.request_extra_form_fields.items():
            form.add_field(str(key), str(value))
        form.add_field(
            "image",
            image_bytes,
            filename="input.png",
            content_type="image/png",
        )
        return form

    async def download_image(self, url: str) -> bytes:
        async def request(session: aiohttp.ClientSession):
            return session.get(url, headers={"User-Agent": "AstrBotImageGenerator/0.1"})

        result = await self._request_with_retry(request)
        if not isinstance(result, bytes):
            raise ImageGenerationError("下载图片结果不是二进制图片")
        self._ensure_output_size(result)
        return result

    async def _post_json(self, path: str, payload: dict[str, Any]) -> bytes | str:
        async def request(session: aiohttp.ClientSession):
            return session.post(
                self._endpoint(path),
                json=payload,
                headers=self._headers(include_json=True),
            )

        return await self._request_with_retry(request)

    async def _post_form(
        self,
        path: str,
        prompt: str,
        image_bytes: bytes,
        image_size: str | None = None,
    ) -> bytes | str:
        async def request(session: aiohttp.ClientSession):
            return session.post(
                self._endpoint(path),
                data=self._build_form(prompt, image_bytes, image_size),
                headers=self._headers(include_json=False),
            )

        return await self._request_with_retry(request)

    async def _request_with_retry(self, request_factory) -> bytes | str:
        attempts = self.config.retry_attempts + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with await request_factory(session) as resp:
                        return await self._parse_response(resp)
            except ImageGenerationHTTPError as exc:
                last_error = exc
                if exc.status not in self.config.retry_status_codes or attempt >= attempts - 1:
                    raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= attempts - 1:
                    raise ImageGenerationError(f"API 请求失败: {exc}") from exc

            await asyncio.sleep(self.config.retry_backoff_seconds * (attempt + 1))

        raise ImageGenerationError(f"API 请求失败: {last_error}")

    async def _parse_response(self, resp: aiohttp.ClientResponse) -> bytes | str:
        content_type = resp.headers.get("Content-Type", "")
        body = await resp.read()
        if resp.status >= 400:
            detail = body[:500].decode("utf-8", errors="ignore")
            raise ImageGenerationHTTPError(resp.status, detail)

        if content_type.startswith("image/"):
            self._ensure_output_size(body)
            return body

        text = body.decode("utf-8", errors="ignore").strip()
        if text.startswith(("http://", "https://", "base64://", "data:image/")):
            return text

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ImageGenerationError("API 返回内容不是图片、URL、base64 或 JSON") from exc

        return self._extract_image_from_json(data)

    def _extract_image_from_json(self, data: Any) -> bytes | str:
        if isinstance(data, str):
            return data

        if isinstance(data, list) and data:
            return self._extract_image_from_json(data[0])

        if not isinstance(data, dict):
            raise ImageGenerationError("API JSON 响应格式无法识别")

        direct_fields = (
            "url",
            "image_url",
            "output_url",
            "b64_json",
            "base64",
            "image_base64",
            "image",
        )
        for field in direct_fields:
            value = data.get(field)
            if isinstance(value, str) and value:
                return value

        for field in ("data", "result", "results", "images", "output"):
            value = data.get(field)
            if value:
                return self._extract_image_from_json(value)

        raise ImageGenerationError("API JSON 响应中未找到图片结果字段")

    def _endpoint(self, path: str) -> str:
        if not self.config.api_base_url:
            raise ImageGenerationError("尚未配置 API Base URL")
        return urljoin(self.config.api_base_url.rstrip("/") + "/", path.lstrip("/"))

    def _headers(self, include_json: bool) -> dict[str, str]:
        headers: dict[str, str] = dict(self.config.custom_headers)
        if include_json:
            headers["Content-Type"] = "application/json"
        if self.config.api_key and self.config.auth_header_name:
            auth_value = self.config.api_key
            if self.config.auth_scheme:
                auth_value = f"{self.config.auth_scheme} {auth_value}"
            headers[self.config.auth_header_name] = auth_value
        return headers

    def _ensure_output_size(self, image_bytes: bytes) -> None:
        if len(image_bytes) > self.config.max_output_image_bytes:
            raise ImageGenerationError("生成图片超过最大输出大小限制")


class AstrBotImageAdapter:
    def __init__(self, client: ImageGenerationClient):
        self.client = client
        self.cache_dir = Path(tempfile.gettempdir()) / "astrbot_plugin_image_generator"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def extract_first_image_bytes(self, event: AstrMessageEvent) -> bytes | None:
        for segment in self._message_segments(event):
            if not self._is_image_segment(segment):
                continue
            image_bytes = await self._segment_to_bytes(segment)
            if image_bytes:
                return image_bytes
        return None

    def has_image(self, event: AstrMessageEvent) -> bool:
        return any(self._is_image_segment(segment) for segment in self._message_segments(event))

    async def result_to_local_image(self, result: bytes | str) -> str:
        if isinstance(result, bytes):
            self.client._ensure_output_size(result)
            return await self._write_image_bytes(result)

        value = result.strip()
        if value.startswith(("http://", "https://")):
            image_bytes = await self.client.download_image(value)
            return await self._write_image_bytes(image_bytes)

        if value.startswith("file://"):
            return value.removeprefix("file://")

        path = Path(value)
        if path.exists():
            return str(path)

        image_bytes = self._decode_base64_image(value)
        self.client._ensure_output_size(image_bytes)
        return await self._write_image_bytes(image_bytes)

    def _message_segments(self, event: AstrMessageEvent) -> list[Any]:
        message_obj = getattr(event, "message_obj", None)
        segments = getattr(message_obj, "message", None)
        if isinstance(segments, list):
            return segments
        return []

    def _is_image_segment(self, segment: Any) -> bool:
        segment_type = getattr(segment, "type", "")
        if str(segment_type).lower().endswith("image"):
            return True
        return segment.__class__.__name__.lower() == "image"

    async def _segment_to_bytes(self, segment: Any) -> bytes | None:
        converter = getattr(segment, "convert_to_file_path", None)
        if callable(converter):
            try:
                converted = converter()
                if inspect.isawaitable(converted):
                    converted = await converted
                if converted:
                    data = await self._reference_to_bytes(str(converted))
                    if data:
                        return data
            except Exception as exc:
                logger.warning(f"通过 AstrBot Image.convert_to_file_path 获取图片失败: {exc}")

        for attr in ("url", "file", "path"):
            value = getattr(segment, attr, None)
            if not value:
                continue
            data = await self._reference_to_bytes(str(value))
            if data:
                return data
        return None

    async def _reference_to_bytes(self, value: str) -> bytes | None:
        if value.startswith(("http://", "https://")):
            return await self.client.download_image(value)
        if value.startswith("file://"):
            return await asyncio.to_thread(Path(value.removeprefix("file://")).read_bytes)
        if value.startswith(("base64://", "data:image/")):
            return self._decode_base64_image(value)

        path = Path(value)
        if path.exists():
            return await asyncio.to_thread(path.read_bytes)
        return None

    def _decode_base64_image(self, value: str) -> bytes:
        if value.startswith("base64://"):
            value = value.removeprefix("base64://")
        elif value.startswith("data:image/"):
            _, value = value.split(",", 1)
        try:
            return base64.b64decode(value, validate=False)
        except Exception as exc:
            raise ImageGenerationError("图片 base64 解码失败") from exc

    async def _write_image_bytes(self, image_bytes: bytes) -> str:
        suffix = self._detect_suffix(image_bytes)
        digest = hashlib.sha256(image_bytes).hexdigest()[:24]
        path = self.cache_dir / f"{digest}{suffix}"
        if not path.exists():
            await asyncio.to_thread(path.write_bytes, image_bytes)
        return str(path)

    def _detect_suffix(self, image_bytes: bytes) -> str:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            return ".gif"
        if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
            return ".webp"
        return ".png"


def is_text_to_image_request(message: str, extra_keywords: list[str] | None = None) -> bool:
    normalized = _normalize_text(message)
    if not normalized:
        return False
    keywords = TEXT_TO_IMAGE_KEYWORDS + (extra_keywords or [])
    return any(keyword.lower() in normalized for keyword in keywords if keyword)


def is_image_to_image_request(
    message: str,
    has_image: bool,
    extra_keywords: list[str] | None = None,
) -> bool:
    if not has_image:
        return False
    normalized = _normalize_text(message)
    if not normalized:
        return False
    keywords = IMAGE_TO_IMAGE_KEYWORDS + (extra_keywords or [])
    return any(keyword.lower() in normalized for keyword in keywords if keyword)


def is_context_image_reference_request(
    message: str,
    extra_keywords: list[str] | None = None,
) -> bool:
    normalized = _normalize_text(message)
    if not normalized:
        return False
    keywords = (
        CONTEXT_IMAGE_REFERENCE_KEYWORDS
        + CONTEXT_IMAGE_MODIFICATION_KEYWORDS
        + IMAGE_TO_IMAGE_KEYWORDS
        + (extra_keywords or [])
    )
    return any(keyword.lower() in normalized for keyword in keywords if keyword)


def clean_prompt(message: str, keywords: list[str] | None = None) -> str:
    prompt = re.sub(r"^/[a-zA-Z0-9_\-]+\s*", "", message or "").strip()
    prompt, _ = extract_image_size(prompt)
    all_keywords = (
        TEXT_TO_IMAGE_KEYWORDS
        + IMAGE_TO_IMAGE_KEYWORDS
        + CONTEXT_IMAGE_REFERENCE_KEYWORDS
        + (keywords or [])
    )
    for keyword in sorted(set(all_keywords), key=len, reverse=True):
        if not keyword:
            continue
        prompt = re.sub(re.escape(keyword), " ", prompt, flags=re.IGNORECASE)
    prompt = re.sub(r"^\s*(请|麻烦|帮我|给我|我想|想要|可以|能不能|能否|把|将|用|以)+", "", prompt)
    prompt = re.sub(r"^\s*(一张|一个|一幅|一份|张|个|幅|份)\s*", "", prompt)
    prompt = re.sub(r"(图片|图|一张|一个|一下|谢谢|吧|吗|呢)[，。,.!！?？\s]*$", "", prompt)
    prompt = re.sub(r"\s+", " ", prompt).strip(" ：:，,。.!！?？\n\t")
    return prompt[:2000]


def extract_image_size(message: str) -> tuple[str, str | None]:
    text = message or ""
    patterns = [
        r"(?:--size|尺寸|大小|分辨率|画幅|size|resolution)\s*[:：=]?\s*(\d{2,5})\s*[xX×*]\s*(\d{2,5})",
        r"(\d{2,5})\s*[xX×*]\s*(\d{2,5})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        width = int(match.group(1))
        height = int(match.group(2))
        if not _valid_image_dimension(width) or not _valid_image_dimension(height):
            continue
        cleaned = (text[: match.start()] + " " + text[match.end() :]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned, f"{width}x{height}"
    return text, None


def _valid_image_dimension(value: int) -> bool:
    return 64 <= value <= 4096


def _normalize_text(message: str) -> str:
    return re.sub(r"\s+", " ", (message or "").strip().lower())


def _safe_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _safe_float(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _safe_int_list(value: Any, default: list[int]) -> list[int]:
    if not isinstance(value, list):
        return default
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result or default


def _build_client_config(config: AstrBotConfig | dict[str, Any] | None) -> ImageGenerationClientConfig:
    merged = dict(DEFAULT_CONFIG)
    if config:
        merged.update(dict(config))

    adapter_mode = str(merged.get("api_adapter_mode", "generic")).strip().lower()
    if adapter_mode not in {"generic", "openai_like"}:
        adapter_mode = "generic"

    return ImageGenerationClientConfig(
        api_base_url=str(merged.get("api_base_url", "")).strip(),
        api_key=str(merged.get("api_key", "")).strip(),
        text_to_image_path=str(merged.get("text_to_image_path", "")).strip(),
        image_to_image_path=str(merged.get("image_to_image_path", "")).strip(),
        model=str(merged.get("model", "")).strip(),
        default_image_size=str(merged.get("default_image_size") or merged.get("image_size") or "1024x1024").strip(),
        timeout_seconds=_safe_int(merged.get("timeout_seconds"), 60),
        max_output_image_bytes=_safe_int(merged.get("max_output_image_bytes"), 20 * 1024 * 1024),
        api_adapter_mode=adapter_mode,
        negative_prompt=str(merged.get("negative_prompt", "")).strip(),
        quality=str(merged.get("quality", "")).strip(),
        style=str(merged.get("style", "")).strip(),
        seed=str(merged.get("seed", "")).strip(),
        steps=_safe_int(merged.get("steps"), 0, minimum=0),
        guidance_scale=_safe_float(merged.get("guidance_scale"), 0.0),
        output_format=str(merged.get("output_format", "")).strip(),
        request_extra_json=_safe_dict(merged.get("request_extra_json")),
        request_extra_form_fields=_safe_dict(merged.get("request_extra_form_fields")),
        custom_headers={str(k): str(v) for k, v in _safe_dict(merged.get("custom_headers")).items()},
        auth_header_name=str(merged.get("auth_header_name", "Authorization")).strip(),
        auth_scheme=str(merged.get("auth_scheme", "Bearer")).strip(),
        retry_attempts=_safe_int(merged.get("retry_attempts"), 2, minimum=0),
        retry_backoff_seconds=_safe_float(merged.get("retry_backoff_seconds"), 1.0),
        retry_status_codes=_safe_int_list(merged.get("retry_status_codes"), [429, 500, 502, 503, 504]),
    )


@register(
    "astrbot_plugin_image_generator",
    "YARIZM",
    "识别聊天中的文生图和图生图需求，并调用外部图片生成 API 返回结果。",
    "0.3.0",
)
class ImageGeneratorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.client = ImageGenerationClient(_build_client_config(self.config))
        self.image_adapter = AstrBotImageAdapter(self.client)
        self.recent_images = RecentImageStore(
            ttl_seconds=_safe_int(self._config_get("recent_image_ttl_seconds", 600), 600),
            max_images_per_session=_safe_int(self._config_get("max_cached_images_per_session", 1), 1),
            max_image_bytes=_safe_int(self._config_get("max_input_image_bytes", 10 * 1024 * 1024), 10 * 1024 * 1024),
        )
        max_concurrency = _safe_int(self._config_get("max_concurrent_generations", 2), 2)
        self.generation_semaphore = asyncio.Semaphore(max_concurrency)
        self.session_last_generation_at: dict[str, float] = {}
        self.failure_count = 0
        self.circuit_open_until = 0.0
        logger.info("astrbot_plugin_image_generator 已加载")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not bool(self._config_get("auto_recognize", True)):
            return

        message = str(getattr(event, "message_str", "") or "")
        if message.strip().startswith(("/imagegen", "/img2img", "/imagegen_clear")):
            return

        text_extra_keywords = self._extra_keywords("trigger_keywords")
        image_extra_keywords = self._extra_keywords("image_to_image_trigger_keywords")
        session_key = self._session_key(event)
        has_image_segment = self.image_adapter.has_image(event)
        current_image, image_error = await self._read_and_cache_current_image(event, session_key)
        cached_image = self._get_cached_image(session_key)

        if image_error and is_image_to_image_request(message, has_image_segment, image_extra_keywords):
            yield event.plain_result(image_error)
            return

        if current_image and is_image_to_image_request(message, True, image_extra_keywords):
            async for result in self._handle_image_to_image(event, message, image_extra_keywords, current_image):
                yield result
            return

        if (
            not current_image
            and is_context_image_reference_request(message, image_extra_keywords)
            and bool(self._config_get("enable_context_image_reference", True))
        ):
            if cached_image:
                async for result in self._handle_image_to_image(
                    event,
                    message,
                    image_extra_keywords,
                    cached_image.image_bytes,
                ):
                    yield result
            else:
                yield event.plain_result(self._message("missing_image_message", "请先发送图片，或将图片和修改要求放在同一条消息中。"))
            return

        if not has_image_segment and is_text_to_image_request(message, text_extra_keywords):
            async for result in self._handle_text_to_image(event, message, text_extra_keywords):
                yield result

    @filter.command("imagegen")
    async def imagegen(self, event: AstrMessageEvent):
        """手动触发文生图：/imagegen 一只赛博朋克风格的猫"""
        message = str(getattr(event, "message_str", "") or "")
        async for result in self._handle_text_to_image(event, message, self._extra_keywords("trigger_keywords")):
            yield result

    @filter.command("img2img")
    async def img2img(self, event: AstrMessageEvent):
        """手动触发图生图：发送图片并附带 /img2img 改成水彩风格"""
        message = str(getattr(event, "message_str", "") or "")
        session_key = self._session_key(event)
        current_image, image_error = await self._read_and_cache_current_image(event, session_key)
        if image_error:
            yield event.plain_result(image_error)
            return

        cached_image = self._get_cached_image(session_key)
        image_bytes = current_image or (cached_image.image_bytes if cached_image else None)
        async for result in self._handle_image_to_image(
            event,
            message,
            self._extra_keywords("image_to_image_trigger_keywords"),
            image_bytes,
        ):
            yield result

    @filter.command("imagegen_clear")
    async def imagegen_clear(self, event: AstrMessageEvent):
        """清除当前会话最近图片缓存。"""
        cleared = self.recent_images.clear(self._session_key(event))
        if cleared:
            yield event.plain_result("已清除当前会话的最近图片缓存。")
        else:
            yield event.plain_result("当前会话没有可清除的图片缓存。")

    async def _handle_text_to_image(
        self,
        event: AstrMessageEvent,
        message: str,
        extra_keywords: list[str],
    ):
        prompt = clean_prompt(message, extra_keywords)
        if not prompt:
            yield event.plain_result("请补充要生成的图片描述。")
            return
        _, image_size = extract_image_size(message)

        guard_message = self._generation_guard(event)
        if guard_message:
            yield event.plain_result(guard_message)
            return

        yield event.plain_result(self._message("generation_progress_message", "正在生成图片，请稍等……"))
        try:
            async with self.generation_semaphore:
                logger.info(f"开始文生图，prompt 长度: {len(prompt)}")
                api_result = await self.client.text_to_image(prompt, image_size=image_size)
                image_path = await self.image_adapter.result_to_local_image(api_result)
            self._record_generation_success()
            yield event.image_result(image_path)
        except Exception as exc:
            self._record_generation_failure()
            logger.exception(f"文生图失败: {exc}")
            yield event.plain_result(self._message("generation_failed_message", "图片生成失败，请稍后再试。"))

    async def _handle_image_to_image(
        self,
        event: AstrMessageEvent,
        message: str,
        extra_keywords: list[str],
        image_bytes: bytes | None,
    ):
        prompt = clean_prompt(message, extra_keywords)
        if not prompt:
            yield event.plain_result("请补充希望如何修改这张图。")
            return
        _, image_size = extract_image_size(message)

        if not image_bytes:
            yield event.plain_result(self._message("missing_image_message", "请先发送图片，或将图片和修改要求放在同一条消息中。"))
            return

        guard_message = self._generation_guard(event)
        if guard_message:
            yield event.plain_result(guard_message)
            return

        yield event.plain_result(self._message("generation_progress_message", "正在生成图片，请稍等……"))
        try:
            async with self.generation_semaphore:
                logger.info(f"开始图生图，prompt 长度: {len(prompt)}，输入图片大小: {len(image_bytes)} bytes")
                api_result = await self.client.image_to_image(prompt, image_bytes, image_size=image_size)
                image_path = await self.image_adapter.result_to_local_image(api_result)
            self._record_generation_success()
            yield event.image_result(image_path)
        except Exception as exc:
            self._record_generation_failure()
            logger.exception(f"图生图失败: {exc}")
            if bool(self._config_get("fallback_text_to_image_on_img2img_failure", False)):
                async for result in self._fallback_text_to_image(event, prompt, image_size=image_size):
                    yield result
                return
            yield event.plain_result(self._message("generation_failed_message", "图片生成失败，请稍后再试。"))

    async def _fallback_text_to_image(self, event: AstrMessageEvent, prompt: str, image_size: str | None = None):
        try:
            async with self.generation_semaphore:
                api_result = await self.client.text_to_image(prompt, image_size=image_size)
                image_path = await self.image_adapter.result_to_local_image(api_result)
            self._record_generation_success()
            yield event.plain_result(self._message("fallback_notice_message", "图生图失败，已尝试按文本描述生成。"))
            yield event.image_result(image_path)
        except Exception as exc:
            self._record_generation_failure()
            logger.exception(f"图生图回退文生图失败: {exc}")
            yield event.plain_result(self._message("generation_failed_message", "图片生成失败，请稍后再试。"))

    async def _read_and_cache_current_image(
        self,
        event: AstrMessageEvent,
        session_key: str,
    ) -> tuple[bytes | None, str | None]:
        if not self.image_adapter.has_image(event):
            return None, None

        image_bytes = await self.image_adapter.extract_first_image_bytes(event)
        if not image_bytes:
            return None, "没有读取到原始图片，请重新发送图片后再试。"

        max_input_bytes = _safe_int(self._config_get("max_input_image_bytes", 10 * 1024 * 1024), 10 * 1024 * 1024)
        if len(image_bytes) > max_input_bytes:
            return None, self._message("input_too_large_message", "图片过大，请压缩后再试。")

        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "")
        if bool(self._config_get("enable_context_image_reference", True)):
            self.recent_images.put(session_key, image_bytes, message_id=message_id)
        return image_bytes, None

    def _get_cached_image(self, session_key: str) -> CachedImage | None:
        if not bool(self._config_get("enable_context_image_reference", True)):
            return None
        return self.recent_images.get(session_key)

    def _session_key(self, event: AstrMessageEvent) -> str:
        unified_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if unified_origin:
            return unified_origin

        message_obj = getattr(event, "message_obj", None)
        for attr in ("session_id", "group_id"):
            value = str(getattr(message_obj, attr, "") or "").strip()
            if value:
                return value
        return "default"

    def _generation_guard(self, event: AstrMessageEvent) -> str | None:
        now = time.monotonic()
        if now < self.circuit_open_until:
            return self._message("service_unavailable_message", "生图服务暂时不可用，请稍后再试。")

        cooldown_seconds = _safe_int(self._config_get("session_cooldown_seconds", 0), 0, minimum=0)
        if cooldown_seconds > 0:
            session_key = self._session_key(event)
            last_at = self.session_last_generation_at.get(session_key, 0.0)
            if now - last_at < cooldown_seconds:
                return self._message("cooldown_message", "请求过于频繁，请稍后再试。")
            self.session_last_generation_at[session_key] = now
        return None

    def _record_generation_success(self) -> None:
        self.failure_count = 0
        self.circuit_open_until = 0.0

    def _record_generation_failure(self) -> None:
        threshold = _safe_int(self._config_get("failure_circuit_breaker_threshold", 3), 3)
        if threshold <= 0:
            return
        self.failure_count += 1
        if self.failure_count >= threshold:
            open_seconds = _safe_int(self._config_get("failure_circuit_breaker_seconds", 60), 60)
            self.circuit_open_until = time.monotonic() + open_seconds

    def _config_get(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _message(self, key: str, default: str) -> str:
        value = self._config_get(key, default)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return default

    def _extra_keywords(self, key: str) -> list[str]:
        value = self._config_get(key, [])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return []

    async def terminate(self):
        logger.info("astrbot_plugin_image_generator 已卸载")
