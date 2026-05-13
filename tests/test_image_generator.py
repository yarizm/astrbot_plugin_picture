from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


PNG_PREFIX = b"\x89PNG\r\n\x1a\n"


def load_plugin_module():
    api_module = types.ModuleType("astrbot.api")
    api_module.AstrBotConfig = dict

    class Logger:
        def info(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def exception(self, *_args, **_kwargs):
            pass

    api_module.logger = Logger()

    event_module = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class EventMessageType:
        ALL = "all"

    class Filter:
        def event_message_type(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

        def command(self, *_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    Filter.EventMessageType = EventMessageType
    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.filter = Filter()

    star_module = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*_args, **_kwargs):
        def decorator(cls):
            return cls

        return decorator

    star_module.Context = Context
    star_module.Star = Star
    star_module.register = register

    sys.modules["astrbot"] = types.ModuleType("astrbot")
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module

    module_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("image_generator_main", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ImageSegment:
    type = "image"

    def __init__(self, image_bytes: bytes):
        self.path = Path(tempfile.mkdtemp()) / "source.png"
        self.path.write_bytes(image_bytes)

    def convert_to_file_path(self):
        return f"file://{self.path}"


class MessageObject:
    def __init__(self, segments=None, message_id="message-1", session_id="session-1", group_id=""):
        self.message = segments or []
        self.message_id = message_id
        self.session_id = session_id
        self.group_id = group_id


class Event:
    def __init__(self, message_str="", segments=None, origin="origin-1", message_id="message-1"):
        self.message_str = message_str
        self.unified_msg_origin = origin
        self.message_obj = MessageObject(segments=segments, message_id=message_id)

    def plain_result(self, text):
        return ("plain", text)

    def image_result(self, path):
        return ("image", path)


async def collect(async_iterable):
    result = []
    async for item in async_iterable:
        result.append(item)
    return result


class ImageGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = load_plugin_module()

    def make_plugin(self, config=None):
        plugin = self.plugin.ImageGeneratorPlugin(self.plugin.Context(), config or {})
        plugin.image_adapter.cache_dir = Path(tempfile.mkdtemp())
        return plugin

    def client_config(self, **overrides):
        data = {
            "api_base_url": "https://api.example.com",
            "api_key": "",
            "text_to_image_path": "/text",
            "image_to_image_path": "/image",
            "model": "model",
            "default_image_size": "1024x1024",
            "timeout_seconds": 30,
            "max_output_image_bytes": 1024,
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
            "retry_attempts": 0,
            "retry_backoff_seconds": 0.0,
            "retry_status_codes": [429, 500, 502, 503, 504],
        }
        data.update(overrides)
        return self.plugin.ImageGenerationClientConfig(**data)

    def install_fake_generation(self, plugin):
        calls = {"text": [], "image": []}

        async def text_to_image(prompt, image_size=None):
            calls["text"].append((prompt, image_size))
            return PNG_PREFIX + b"text-result"

        async def image_to_image(prompt, image_bytes, image_size=None):
            calls["image"].append((prompt, image_bytes, image_size))
            return PNG_PREFIX + b"image-result"

        plugin.client.text_to_image = text_to_image
        plugin.client.image_to_image = image_to_image
        return calls

    def test_text_to_image_intent(self):
        self.assertTrue(self.plugin.is_text_to_image_request("帮我生成图片：一只猫"))
        self.assertTrue(self.plugin.is_text_to_image_request("draw a robot"))
        self.assertTrue(self.plugin.is_text_to_image_request("做一张海报", ["做一张"]))
        self.assertFalse(self.plugin.is_text_to_image_request("今天天气怎么样"))

    def test_image_to_image_requires_image(self):
        self.assertTrue(self.plugin.is_image_to_image_request("把这张图改成水彩风格", True))
        self.assertTrue(self.plugin.is_image_to_image_request("image to image, pixel art", True))
        self.assertFalse(self.plugin.is_image_to_image_request("帮我画一张猫", True))
        self.assertFalse(self.plugin.is_image_to_image_request("把这张图改成水彩风格", False))
        self.assertFalse(self.plugin.is_image_to_image_request("普通聊天", True))

    def test_context_image_reference_intent(self):
        self.assertTrue(self.plugin.is_context_image_reference_request("把上面那张图改成水彩风格"))
        self.assertTrue(self.plugin.is_context_image_reference_request("改成油画风格"))
        self.assertFalse(self.plugin.is_context_image_reference_request("帮我画一张猫"))

    def test_clean_prompt_removes_commands_keywords_and_context_words(self):
        prompt = self.plugin.clean_prompt("/imagegen 帮我画一张 赛博朋克城市 图片")
        self.assertEqual(prompt, "赛博朋克城市")

        prompt = self.plugin.clean_prompt("/img2img 把上面那张图改成 水彩插画风格")
        self.assertEqual(prompt, "水彩插画风格")

    def test_user_image_size_is_extracted_and_removed_from_prompt(self):
        prompt, image_size = self.plugin.extract_image_size("帮我画一张猫，尺寸 768x1024")
        self.assertEqual(prompt, "帮我画一张猫，")
        self.assertEqual(image_size, "768x1024")

        prompt, image_size = self.plugin.extract_image_size("/imagegen cat --size 512x512")
        self.assertEqual(prompt, "/imagegen cat")
        self.assertEqual(image_size, "512x512")

        prompt = self.plugin.clean_prompt("/imagegen 帮我画一张猫 1024x1024")
        self.assertEqual(prompt, "猫")

    def test_extract_image_from_common_json_shapes(self):
        config = self.client_config(
            api_key="secret",
        )
        client = self.plugin.ImageGenerationClient(config)

        self.assertEqual(
            client._extract_image_from_json({"data": [{"url": "https://cdn.example.com/a.png"}]}),
            "https://cdn.example.com/a.png",
        )
        self.assertEqual(
            client._extract_image_from_json({"result": {"b64_json": "YWJj"}}),
            "YWJj",
        )

        with self.assertRaises(self.plugin.ImageGenerationError):
            client._extract_image_from_json({"message": "no image"})

    def test_adapter_decodes_and_writes_image_results(self):
        config = self.client_config()
        client = self.plugin.ImageGenerationClient(config)
        adapter = self.plugin.AstrBotImageAdapter(client)
        adapter.cache_dir = Path(tempfile.mkdtemp())

        png_bytes = PNG_PREFIX + b"test"
        path = asyncio.run(adapter.result_to_local_image(png_bytes))

        self.assertTrue(path.endswith(".png"))
        self.assertEqual(Path(path).read_bytes(), png_bytes)

    def test_adapter_reads_file_url_from_converted_image_segment(self):
        image_bytes = PNG_PREFIX + b"source"
        event = Event(segments=[ImageSegment(image_bytes)])
        config = self.client_config()
        adapter = self.plugin.AstrBotImageAdapter(self.plugin.ImageGenerationClient(config))

        result = asyncio.run(adapter.extract_first_image_bytes(event))
        self.assertEqual(result, image_bytes)

    def test_context_image_to_image_uses_cached_previous_image(self):
        plugin = self.make_plugin()
        calls = self.install_fake_generation(plugin)
        original = PNG_PREFIX + b"cached"

        first = Event(segments=[ImageSegment(original)])
        self.assertEqual(asyncio.run(collect(plugin.on_message(first))), [])

        second = Event(message_str="把上面那张图改成水彩风格")
        results = asyncio.run(collect(plugin.on_message(second)))

        self.assertEqual(calls["image"], [("水彩风格", original, None)])
        self.assertEqual(results[0], ("plain", "正在生成图片，请稍等……"))
        self.assertEqual(results[1][0], "image")

    def test_current_image_takes_priority_and_refreshes_cache(self):
        plugin = self.make_plugin()
        calls = self.install_fake_generation(plugin)
        cached = PNG_PREFIX + b"cached"
        current = PNG_PREFIX + b"current"

        asyncio.run(collect(plugin.on_message(Event(segments=[ImageSegment(cached)]))))
        results = asyncio.run(
            collect(plugin.on_message(Event(message_str="改成油画风格", segments=[ImageSegment(current)])))
        )

        self.assertEqual(calls["image"], [("油画风格", current, None)])
        self.assertEqual(plugin.recent_images.get("origin-1").image_bytes, current)
        self.assertEqual(results[1][0], "image")

    def test_expired_cache_returns_missing_image_prompt(self):
        plugin = self.make_plugin({"recent_image_ttl_seconds": 1})
        calls = self.install_fake_generation(plugin)

        asyncio.run(collect(plugin.on_message(Event(segments=[ImageSegment(PNG_PREFIX + b"old")]))))
        cached = plugin.recent_images.get("origin-1")
        self.assertIsNotNone(cached)
        cached.created_at -= 2

        results = asyncio.run(collect(plugin.on_message(Event(message_str="把上面那张图改成水彩风格"))))

        self.assertEqual(calls["image"], [])
        self.assertEqual(results, [("plain", "请先发送图片，或将图片和修改要求放在同一条消息中。")])

    def test_imagegen_clear_removes_cached_image(self):
        plugin = self.make_plugin()

        asyncio.run(collect(plugin.on_message(Event(segments=[ImageSegment(PNG_PREFIX + b"cached")]))))
        self.assertIsNotNone(plugin.recent_images.get("origin-1"))

        results = asyncio.run(collect(plugin.imagegen_clear(Event())))

        self.assertEqual(results, [("plain", "已清除当前会话的最近图片缓存。")])
        self.assertIsNone(plugin.recent_images.get("origin-1"))

    def test_image_only_message_caches_without_generation(self):
        plugin = self.make_plugin()
        calls = self.install_fake_generation(plugin)

        results = asyncio.run(collect(plugin.on_message(Event(segments=[ImageSegment(PNG_PREFIX + b"only")]))))

        self.assertEqual(results, [])
        self.assertEqual(calls["image"], [])
        self.assertIsNotNone(plugin.recent_images.get("origin-1"))

    def test_text_to_image_not_misrouted_when_cache_exists(self):
        plugin = self.make_plugin()
        calls = self.install_fake_generation(plugin)

        asyncio.run(collect(plugin.on_message(Event(segments=[ImageSegment(PNG_PREFIX + b"cached")]))))
        results = asyncio.run(collect(plugin.on_message(Event(message_str="帮我画一张猫"))))

        self.assertEqual(calls["image"], [])
        self.assertEqual(calls["text"], [("猫", None)])
        self.assertEqual(results[1][0], "image")

    def test_oversized_input_image_is_not_cached_or_generated(self):
        plugin = self.make_plugin({"max_input_image_bytes": 8})
        calls = self.install_fake_generation(plugin)

        results = asyncio.run(
            collect(plugin.on_message(Event(message_str="改成水彩风格", segments=[ImageSegment(PNG_PREFIX + b"too-big")])))
        )

        self.assertEqual(calls["image"], [])
        self.assertIsNone(plugin.recent_images.get("origin-1"))
        self.assertEqual(results, [("plain", "图片过大，请压缩后再试。")])

    def test_custom_request_options_are_included_in_payload(self):
        config = self.client_config(
            api_key="secret",
            negative_prompt="low quality",
            quality="hd",
            style="natural",
            seed="42",
            steps=20,
            guidance_scale=7.5,
            output_format="b64_json",
            request_extra_json={"sampler": "dpmpp"},
            custom_headers={"X-Trace": "yes"},
            auth_header_name="X-API-Key",
            auth_scheme="",
        )
        client = self.plugin.ImageGenerationClient(config)

        payload = client._base_payload("cat")
        custom_size_payload = client._base_payload("cat", image_size="768x1024")
        headers = client._headers(include_json=True)

        self.assertEqual(payload["size"], "1024x1024")
        self.assertEqual(custom_size_payload["size"], "768x1024")
        self.assertEqual(payload["negative_prompt"], "low quality")
        self.assertEqual(payload["quality"], "hd")
        self.assertEqual(payload["style"], "natural")
        self.assertEqual(payload["seed"], "42")
        self.assertEqual(payload["steps"], 20)
        self.assertEqual(payload["guidance_scale"], 7.5)
        self.assertEqual(payload["response_format"], "b64_json")
        self.assertEqual(headers["X-Trace"], "yes")
        self.assertEqual(headers["X-API-Key"], "secret")

    def test_cooldown_blocks_repeated_generation(self):
        plugin = self.make_plugin({"session_cooldown_seconds": 60})
        calls = self.install_fake_generation(plugin)

        first = asyncio.run(collect(plugin.on_message(Event(message_str="帮我画一张猫"))))
        second = asyncio.run(collect(plugin.on_message(Event(message_str="帮我画一张狗"))))

        self.assertEqual(calls["text"], [("猫", None)])
        self.assertEqual(first[1][0], "image")
        self.assertEqual(second, [("plain", "请求过于频繁，请稍后再试。")])

    def test_user_size_is_passed_to_text_and_image_generation(self):
        plugin = self.make_plugin()
        calls = self.install_fake_generation(plugin)

        asyncio.run(collect(plugin.on_message(Event(message_str="帮我画一张猫 768x1024"))))
        asyncio.run(
            collect(
                plugin.on_message(
                    Event(
                        message_str="改成水彩风格 --size 512x512",
                        segments=[ImageSegment(PNG_PREFIX + b"img")],
                    )
                )
            )
        )

        self.assertEqual(calls["text"], [("猫", "768x1024")])
        self.assertEqual(calls["image"], [("水彩风格", PNG_PREFIX + b"img", "512x512")])

    def test_circuit_breaker_opens_after_failures(self):
        plugin = self.make_plugin(
            {
                "failure_circuit_breaker_threshold": 1,
                "failure_circuit_breaker_seconds": 60,
            }
        )

        async def fail_text_to_image(_prompt, image_size=None):
            raise RuntimeError("boom")

        plugin.client.text_to_image = fail_text_to_image

        first = asyncio.run(collect(plugin.on_message(Event(message_str="帮我画一张猫"))))
        second = asyncio.run(collect(plugin.on_message(Event(message_str="帮我画一张狗"))))

        self.assertEqual(first[-1], ("plain", "图片生成失败，请稍后再试。"))
        self.assertEqual(second, [("plain", "生图服务暂时不可用，请稍后再试。")])

    def test_img2img_can_fallback_to_text_to_image(self):
        plugin = self.make_plugin({"fallback_text_to_image_on_img2img_failure": True})
        calls = {"text": [], "image": []}

        async def fail_image_to_image(prompt, image_bytes, image_size=None):
            calls["image"].append((prompt, image_bytes, image_size))
            raise RuntimeError("img2img failed")

        async def text_to_image(prompt, image_size=None):
            calls["text"].append((prompt, image_size))
            return PNG_PREFIX + b"fallback"

        plugin.client.image_to_image = fail_image_to_image
        plugin.client.text_to_image = text_to_image

        results = asyncio.run(
            collect(plugin.on_message(Event(message_str="改成水彩风格", segments=[ImageSegment(PNG_PREFIX + b"img")])))
        )

        self.assertEqual(calls["text"], [("水彩风格", None)])
        self.assertEqual(results[-2], ("plain", "图生图失败，已尝试按文本描述生成。"))
        self.assertEqual(results[-1][0], "image")


if __name__ == "__main__":
    unittest.main()
