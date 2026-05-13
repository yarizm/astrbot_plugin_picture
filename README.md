# AstrBot 图片生成插件

该插件用于在 AstrBot 聊天消息中识别图片生成需求，调用外部图片生成 API，并把生成结果作为图片消息返回。

## 功能

- 文生图：识别“生成图片”“画一张”“帮我画”“出图”“draw”“generate image”等表达。
- 图生图：识别“图生图”“改图”“参考这张图”“改成”“风格化”等表达。
- 上下文图生图：用户先发送图片，随后发送“把上面那张图改成水彩风格”等文本时，会使用当前会话最近一张图片。
- 支持 API 返回图片 URL、base64、`base64://`、`data:image/...` 或图片二进制响应。
- 无法识别为图片生成请求时不响应普通聊天。

## 文件结构

```text
.
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── README.md
├── requirements.txt
├── LICENSE
├── .github/workflows/ci.yml
└── tests/
    └── test_image_generator.py
```

## 安装

1. 将本仓库目录放入 AstrBot 的 `data/plugins/` 目录。
2. 安装依赖：

```bash
cd data/plugins/<插件目录名>
pip install -r requirements.txt
```

3. 在 AstrBot WebUI 的插件管理中重载插件，或重启 AstrBot。

## 配置

插件提供 `_conf_schema.json`，AstrBot 会在 WebUI 中生成配置项。

- `api_base_url`：外部生图服务基础地址，例如 `https://api.example.com`
- `api_key`：外部 API Key，不要写入代码
- `text_to_image_path`：文生图接口路径，默认 `/v1/images/generations`
- `image_to_image_path`：图生图接口路径，默认 `/v1/images/edits`
- `model`：模型名称
- `default_image_size`：默认图片尺寸。用户未在消息中指定尺寸时使用。
- `timeout_seconds`：请求超时时间，默认 `60`
- `auto_recognize`：是否启用普通聊天自动识别，默认开启
- `trigger_keywords`：额外文生图触发关键词列表
- `image_to_image_trigger_keywords`：额外图生图触发关键词列表
- `enable_context_image_reference`：是否启用上下文图片引用，默认开启
- `recent_image_ttl_seconds`：最近图片缓存时间，默认 `600`
- `max_cached_images_per_session`：每个会话缓存图片数量，默认 `1`
- `max_input_image_bytes`：输入图片大小限制，默认 `10485760`
- `max_output_image_bytes`：输出图片大小限制，默认 `20971520`
- `max_concurrent_generations`：最大并发生成数，默认 `2`
- `api_adapter_mode`：API 适配模式，支持 `generic` 和 `openai_like`
- `negative_prompt`、`quality`、`style`、`seed`、`steps`、`guidance_scale`、`output_format`：常见生图参数，留空或为 0 时不会发送。
- `request_extra_json`：附加到文生图 JSON 请求体的自定义字段。
- `request_extra_form_fields`：附加到图生图 multipart form 的自定义字段。
- `custom_headers`、`auth_header_name`、`auth_scheme`：用于适配不同 API 的请求头和鉴权方式。
- `retry_attempts`、`retry_backoff_seconds`、`retry_status_codes`：网络错误和指定 HTTP 状态码的重试策略。
- `session_cooldown_seconds`：同一会话生成冷却时间，默认 `0` 表示不限制。
- `failure_circuit_breaker_threshold`、`failure_circuit_breaker_seconds`：连续失败后的临时熔断保护。
- `fallback_text_to_image_on_img2img_failure`：图生图 API 失败时是否尝试回退到文生图，默认关闭。
- `generation_progress_message`、`generation_failed_message`、`missing_image_message` 等：可自定义用户提示文案。

## 命令

- `/imagegen <prompt>`：手动触发文生图。
- `/img2img <prompt>`：手动触发图生图，优先使用当前消息中的图片，否则使用当前会话最近图片。
- `/imagegen_clear`：清除当前会话最近图片缓存。

## API 适配

通用 API 封装位于 `main.py` 的 `ImageGenerationClient`。

- `generic`：文生图发送 JSON，图生图发送 multipart form。
- `openai_like`：文生图请求会附加 `n=1`，响应解析兼容常见 OpenAI 风格的 `data[0].url`、`data[0].b64_json`。
- `_extract_image_from_json()` 默认解析：`url`、`image_url`、`output_url`、`b64_json`、`base64`、`image_base64`、`image`、`data`、`result`、`results`、`images`、`output`。

如果真实 API 的请求或响应字段不同，通常只需要调整 `ImageGenerationClient` 中的请求参数和响应解析。

## 回退保护

- 重试：默认对网络错误和 `429/500/502/503/504` 重试 2 次。
- 冷却：可通过 `session_cooldown_seconds` 限制同一会话连续生图。
- 熔断：连续失败达到 `failure_circuit_breaker_threshold` 后，会在指定时间内停止调用外部 API。
- 大小限制：输入和输出图片分别受 `max_input_image_bytes`、`max_output_image_bytes` 限制。
- 图生图回退：开启 `fallback_text_to_image_on_img2img_failure` 后，图生图失败会尝试用同一 prompt 走文生图。

## 使用示例

文生图：

```text
帮我画一张赛博朋克城市夜景，霓虹灯，雨天，电影感
```

指定尺寸：

```text
帮我画一张赛博朋克城市夜景，尺寸 768x1024
```

也支持：

```text
/imagegen 一只猫 --size 1024x1024
```

手动触发文生图：

```text
/imagegen 一只穿宇航服的橘猫，写实风格
```

图生图，同一条消息中发送图片和文本：

```text
把这张图改成水彩插画风格
```

上下文图生图：

```text
先发送一张图片
随后发送：把上面那张图改成像素风头像
```

清除当前会话图片缓存：

```text
/imagegen_clear
```

## 测试

在仓库根目录运行：

```bash
python -m unittest discover -s tests -v
```

也可以做语法检查：

```bash
python -m compileall -q main.py tests
```

测试中会用轻量 stub 替代 AstrBot 运行时模块，因此不需要启动 AstrBot。

## AstrBot 版本差异

不同 AstrBot 版本或平台适配器对图片段字段可能有差异。平台相关逻辑集中在 `AstrBotImageAdapter`。

- `extract_first_image_bytes()`：从消息链读取用户发送的第一张图片。
- `_segment_to_bytes()`：优先使用图片组件的 `convert_to_file_path()`，失败后尝试 `url`、`file`、`path` 字段。
- `result_to_local_image()`：把 API 返回的 URL、base64 或 bytes 转为本地图片路径。

如果你的 AstrBot 版本发送图片 API 不同，优先调整 `_handle_text_to_image()` 和 `_handle_image_to_image()` 中的 `event.image_result(image_path)`。

## 常见问题

### 插件没有响应

检查 `auto_recognize` 是否开启；确认消息中包含内置关键词或配置的触发关键词。

### 提示未配置 API Base URL

需要先在 WebUI 中配置 `api_base_url`，并确认接口路径正确。

### 先发图片后无法改图

检查 `enable_context_image_reference` 是否开启，并确认修改指令在 `recent_image_ttl_seconds` 时间内发送。

### 图生图读取不到图片

不同平台适配器的图片字段可能不同。根据日志调整 `AstrBotImageAdapter._segment_to_bytes()`，将平台图片字段转换为 bytes。

### API 返回 JSON 但解析失败

根据真实响应格式调整 `ImageGenerationClient._extract_image_from_json()`。

## 许可证

本项目使用 MIT License。
