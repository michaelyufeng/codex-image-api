# codex-image-api

[English](README.md) · **简体中文**

把 **Codex / ChatGPT 订阅额度**的生图能力，包装成一个**本地 OpenAI 兼容 HTTP API**。现有调用 OpenAI 图片接口的项目，**只改 `base_url` 一行**就能切过来，省掉每张图的 API 费用。

> ⚠️ **免责声明** — 本项目仅供 **学习研究** 与 **个人复用自己的 ChatGPT/Codex 订阅额度**。它经 Codex CLI 的官方 OAuth 以订阅额度驱动生图，**可能不符合 OpenAI 服务条款**——是否使用、如何使用由你**自行判断并承担全部风险**。**请勿用于商业用途或对外大规模分发。** 作者不对账号处置或任何后果负责。

```
你的项目 (OpenAI SDK) ──▶ http://127.0.0.1:10532/v1/images/generations
                      ──▶ 包装成 Responses API 的 image_generation 工具调用
                      ──▶ https://chatgpt.com/backend-api/codex/responses
                          (复用 ~/.codex/auth.json 的 OAuth token，和 Codex CLI 同款)
                      ──▶ 解析 SSE 流，返回标准 OpenAI Images 格式
```

## 特点

- **零第三方依赖**：纯 Python 标准库，无 telemetry，好审计、好部署。
- **走订阅额度**：只连 `chatgpt.com` + `auth.openai.com`，全程不碰 `api.openai.com` / `OPENAI_API_KEY`。
- **OpenAI 兼容**：`/v1/images/generations`（文生图）、`/v1/images/edits`（图生图）、`/v1/models`。
- **文生图 + 图生图 + 批量 + URL 返回**，并发限流防止被限流。

> ⚠️ **计费与合规** — 本工具复用 Codex CLI 的官方 OAuth 流程（非抓包逆向），属于「灰色地带但用官方认证」。仅供个人复用自己的订阅额度。是否计费请自行在 `platform.openai.com` 用量页核对（正常应只见 ChatGPT 订阅用量、无 Images API 计费）。

## 前置要求

- 已安装 [Codex CLI](https://github.com/openai/codex) 并完成登录（`codex login`），即存在 `~/.codex/auth.json`。
- Python 3.10+（开发环境为 3.14）。

## 安装为 Claude Code 插件（推荐）

本仓库本身就是一个 Claude Code 插件 + 市场。装上后即获得两个 skill——`generate-image`（生图/改图）与 `connect-api`（把现有项目切到本地 API）——后端 server 由 skill **按需自启**，无需手动管理。

在 Claude Code 里执行：

```bash
/plugin marketplace add michaelyufeng/codex-image-api
/plugin install codex-image-api@codex-image-api
```

装好后直接说「画一张赛博朋克风格的猫」即可触发生图；说「把这个项目接入本地生图 API」即可触发接入。skill 脚本用 `${CLAUDE_PLUGIN_ROOT}` 定位插件内的 server，**不依赖任何硬编码路径**。

> 只想要 HTTP API、不需要 skill？跳过本节，直接看下面用 `./run.sh` 起服务。

## 启动服务（仅 HTTP API）

```bash
./run.sh          # 前台运行
./run.sh bg       # 后台运行，日志在 /tmp/codex-image-api.log
./run.sh stop     # 停止后台进程
# 或直接：
python3 server.py
```

默认监听 `http://127.0.0.1:10532`。健康检查：

```bash
curl http://127.0.0.1:10532/health
# {"ok": true, "auth": "expires in 863239s", "model": "gpt-5.5", "concurrency": 3,
#  "active": 0, "queued": 0, "uptime_s": 42, "version": "0.5.0"}
```

## 在你的项目里使用（改一行）

```python
from openai import OpenAI
import base64

client = OpenAI(base_url="http://127.0.0.1:10532/v1", api_key="unused")  # ← 只改这行

# 文生图
r = client.images.generate(model="gpt-image-2", prompt="一只戴橙围巾的水獭", size="1024x1024")
open("out.png", "wb").write(base64.b64decode(r.data[0].b64_json))

# 图生图
r2 = client.images.edit(model="gpt-image-2", image=open("out.png", "rb"),
                        prompt="把围巾改成蓝色")
open("out2.png", "wb").write(base64.b64decode(r2.data[0].b64_json))
```

更多示例见 [`examples/`](./examples/)。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/images/generations` | 文生图（JSON）。支持 `n`、`response_format`、`reference_images` 扩展 |
| `POST` | `/v1/images/edits` | 图生图（multipart，OpenAI SDK `images.edit` 入口） |
| `GET`  | `/images/<name>` | `response_format=url` 时托管生成的图片 |
| `GET`  | `/health` | 服务与 token 状态 |
| `GET`  | `/v1/models` | 模型列表 |

### 请求参数（generations 与 edits 通用）

| 字段 | 默认 | 可选值 |
|---|---|---|
| `prompt` | *（必填）* | 任意文本 |
| `size` | `1024x1024` | `1024x1024` / `1024x1536` / `1536x1024` / `auto` |
| `quality` | `high` | `low` / `medium` / `high` / `auto` |
| `n` | `1` | `1`–`8` |
| `response_format` | `b64_json` | `b64_json` / `url` |
| `output_format` | *（不传 → png）* | `png` / `jpeg` / `webp` |
| `output_compression` | *（不传）* | 整数 `0`–`100`（仅 jpeg/webp 生效） |
| `background` | *（不传）* | 会透传，但上游 `gpt-image-2-codex` 目前**拒绝** `transparent`（实测） |
| `input_fidelity` | *（不传）* | 会透传，但上游 `gpt-image-2-codex` 目前**拒绝**该参数（实测） |
| `moderation` | `low` | `low` / `auto` |
| `effort` | `medium` | `low`（快档，prompt 原样透传）/ `medium`（默认——先思考、把你的意图扩写成摄影 brief 再画）/ `high`（最深思考）。非 `low` 即"深度模式"，明显更真实但慢 ~2–4× |
| `reference_images` | `[]` | 数组，元素为本地路径 / `data:` URL / `{"data","mime"}`（图生图扩展）。`http(s)` URL 默认拒绝，需 `CODEX_IMAGE_ALLOW_REMOTE_REFS=1` 才放行（SSRF 防护） |

可选透传字段（`background`、`output_format` 等）只在显式传入时才转发上游。注意 `effort` 现在默认 `medium`（深度模式）——不传它的调用会得到更精细但更慢的结果、且 prompt 会被自动扩写；要恢复旧的快档透传请显式传 `effort=low`。批量（`n>1`）部分失败时，返回成功的图片并附 `warnings` 数组，不再整单报废。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `CODEX_IMAGE_HOST` | `127.0.0.1` | 监听地址 |
| `CODEX_IMAGE_PORT` | `10532` | 监听端口 |
| `CODEX_IMAGE_MODEL` | `gpt-5.5` | 驱动 image_generation 工具的编排模型 |
| `CODEX_IMAGE_CONCURRENCY` | `3` | 同时打到上游的最大生图数 |
| `CODEX_IMAGE_MAX_N` | `8` | 单次请求 `n` 上限 |
| `CODEX_IMAGE_DIR` | `./generated` | `url` 模式落盘目录 |
| `CODEX_IMAGE_AUTH_FILE` | *（自动）* | 覆盖 `auth.json` 路径 |
| `CODEX_IMAGE_PUBLIC_BASE` | `http://HOST:PORT` | `url` 模式返回的地址前缀 |
| `CODEX_IMAGE_ALLOWED_HOSTS` | *（自动）* | 额外放行的 `Host` 头（逗号分隔）；默认仅放行 localhost，挡 DNS rebinding |
| `CODEX_IMAGE_MAX_BODY` | `67108864` | 单次请求体上限（字节，超出返回 413） |
| `CODEX_IMAGE_MAX_REFS` | `16` | 单次请求 `reference_images` / 上传图片数量上限 |
| `CODEX_IMAGE_ALLOW_REMOTE_REFS` | *（关）* | 设为 `1` 放行 `http(s)` URL 参考图（会转发上游 fetch，有 SSRF 风险，默认关） |
| `CODEX_IMAGE_TIMEOUT` | `400` | 单次上游生成的 socket 超时（秒） |
| `CODEX_IMAGE_RETRIES` | `2` | 上游瞬时故障（断流、401/429/5xx、空结果）的额外重试次数 |

## 稳定性说明

- **客户端超时要给足。** `quality=high` 单张 1–3 分钟；`n>1` 还要按 `CODEX_IMAGE_CONCURRENCY` 分批排队。客户端超时比服务端短就会看到请求"断线"——其实服务端还在干活（日志会出现 `client disconnected before the response could be sent`）。用 OpenAI SDK 时：`OpenAI(base_url=..., api_key="unused", timeout=600)`。
- **内置重试。** 上游 SSE 断流、401（自动换新 token）、429/5xx、空结果都会按退避自动重试，重试用尽才报错。
- **客户端挂了就停手。** 调用方在排队或重试间隙挂断时，服务端会放弃该任务，不再白烧订阅额度。
- **同端口只留一个实例。** launchd KeepAlive、skill 自启、`./run.sh` 可能抢同一端口；现在抢输的一方安静退出，不再 `Address already in use` 崩溃循环刷日志。
- `GET /health` 新增 `active` / `queued`（在产/排队中的生成数）与 `uptime_s`，便于排查。

## 项目结构

```
.claude-plugin/   插件 + 市场清单（plugin.json / marketplace.json）
skills/           捆绑的两个 skill：generate-image（生图）、connect-api（接入）
server.py         HTTP 服务：OpenAI 兼容路由 + 上游 Responses 调用 + SSE 解析 + token 刷新
_codex_auth.py    auth.json 发现 + JWT 解码（零副作用），被 server.py 与 lib_preflight.py 共用
lib_preflight.py  skill 的前置检查：Codex 登录态探测 + 服务自启（自启逻辑唯一来源）
run.sh            控制脚本：前台 / 后台(bg) / 停止(stop) / 安装开机自启(install-launchd)
deploy/*.plist    macOS LaunchAgent 模板（安装时填充真实路径）
examples/         curl + Python(OpenAI SDK) 调用示例
```

> skill 脚本（`skills/*/`）通过 `__file__` 相对定位本仓库根的 `lib_preflight.py` / `server.py`，因此无论插件被安装到哪里都能找到后端；`CODEX_IMAGE_SERVER_DIR` 可覆盖指向另一处源码。

## 开机自启（macOS launchd）

见 [`deploy/`](./deploy/) 下的 LaunchAgent 模板与安装说明。

## 已知限制

- 单账号并发生图由上游排队，`n>1` 实际接近串行耗时。
- `response_format=url` 的图片落在本地 `generated/`，仅本机可访问（已被 `.gitignore` 忽略）。
- 生图较慢，且 `effort` 默认 `medium`（深度模式）：单张约 1–3 分钟；`effort=low` 才是快档（≈ 40s）。属于走 agent 通道的固有开销。
