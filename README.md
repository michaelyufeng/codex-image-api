# codex-image-api

**English** · [简体中文](README.zh-CN.md)

Wrap the image-generation capability of your **Codex / ChatGPT subscription** as a **local, OpenAI-compatible HTTP API**. Any project that already calls the OpenAI image API can switch over by changing **one line — `base_url`** — and stop paying per-image API fees.

> ⚠️ **Disclaimer** — This project is for **learning / research** and **personal reuse of your own ChatGPT/Codex subscription** only. It drives image generation through Codex CLI's official OAuth on your subscription quota, which **may violate OpenAI's Terms of Service**. Whether and how you use it is **entirely at your own risk**. **Do not use it commercially or distribute it widely.** The author is not responsible for account action or any consequences.

```
your project (OpenAI SDK) ──▶ http://127.0.0.1:10532/v1/images/generations
                          ──▶ wrapped as a Responses-API image_generation tool call
                          ──▶ https://chatgpt.com/backend-api/codex/responses
                              (OAuth token from ~/.codex/auth.json — same as Codex CLI)
                          ──▶ parse the SSE stream → standard OpenAI Images JSON
```

## Features

- **Zero third-party dependencies** — pure Python standard library, no telemetry; easy to audit and deploy.
- **Subscription-backed** — talks only to `chatgpt.com` + `auth.openai.com`; never touches `api.openai.com` / `OPENAI_API_KEY`.
- **OpenAI-compatible** — `/v1/images/generations` (text→image), `/v1/images/edits` (image→image), `/v1/models`.
- **Text→image, image→image, batching, URL output**, with concurrency limiting to avoid upstream rate limits.

> ⚠️ **Billing & compliance** — This tool reuses Codex CLI's official OAuth flow (not packet-sniffing / reverse-engineering) — a "gray area, but with official auth." For personal reuse of your own quota only. Verify billing yourself on `platform.openai.com` (you should see only ChatGPT subscription usage, no Images API charges).

## Requirements

- [Codex CLI](https://github.com/openai/codex) installed and logged in (`codex login`) — i.e. `~/.codex/auth.json` exists.
- Python 3.10+ (developed on 3.14).

## Install as a Claude Code plugin (recommended)

This repo is itself a Claude Code plugin + marketplace. Once installed you get two skills — `generate-image` (create / edit images) and `connect-api` (switch an existing project to the local API) — and the backend server is **auto-started on demand** by the skills, so you never manage it by hand.

In Claude Code:

```bash
/plugin marketplace add michaelyufeng/codex-image-api
/plugin install codex-image-api@codex-image-api
```

Then just say *"draw a cyberpunk cat"* to trigger generation, or *"switch this project to the local image API"* to trigger connect. The skill scripts locate the bundled server via `${CLAUDE_PLUGIN_ROOT}` — **no hardcoded paths**.

> Only want the HTTP API, not the skills? Skip this section and run the server with `./run.sh` below.

## Run the server (HTTP-API only)

```bash
./run.sh          # foreground
./run.sh bg       # background, logs at /tmp/codex-image-api.log
./run.sh stop     # stop the background server
# or directly:
python3 server.py
```

Listens on `http://127.0.0.1:10532` by default. Health check:

```bash
curl http://127.0.0.1:10532/health
# {"ok": true, "auth": "expires in 863239s", "model": "gpt-5.4-mini", "concurrency": 3,
#  "active": 0, "queued": 0, "uptime_s": 42, "version": "0.4"}
```

## Use in your project (change one line)

```python
from openai import OpenAI
import base64

client = OpenAI(base_url="http://127.0.0.1:10532/v1", api_key="unused")  # ← only this line

# text → image
r = client.images.generate(model="gpt-image-2", prompt="an otter in an orange scarf", size="1024x1024")
open("out.png", "wb").write(base64.b64decode(r.data[0].b64_json))

# image → image
r2 = client.images.edit(model="gpt-image-2", image=open("out.png", "rb"),
                        prompt="change the scarf to blue")
open("out2.png", "wb").write(base64.b64decode(r2.data[0].b64_json))
```

More in [`examples/`](./examples/).

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/images/generations` | text→image (JSON). Supports `n`, `response_format`, and a `reference_images` extension |
| `POST` | `/v1/images/edits` | image→image (multipart, OpenAI SDK `images.edit` entry point) |
| `GET`  | `/images/<name>` | serves generated images when `response_format=url` |
| `GET`  | `/health` | service + token status |
| `GET`  | `/v1/models` | model list |

### Request parameters (generations & edits)

| Field | Default | Allowed |
|---|---|---|
| `prompt` | *(required)* | any text |
| `size` | `1024x1024` | `1024x1024` / `1024x1536` / `1536x1024` / `auto` |
| `quality` | `high` | `low` / `medium` / `high` / `auto` |
| `n` | `1` | `1`–`8` |
| `response_format` | `b64_json` | `b64_json` / `url` |
| `output_format` | *(unset → png)* | `png` / `jpeg` / `webp` |
| `output_compression` | *(unset)* | integer `0`–`100` (jpeg/webp only) |
| `background` | *(unset)* | passed through, but upstream `gpt-image-2-codex` currently **rejects** `transparent` |
| `input_fidelity` | *(unset)* | passed through, but upstream `gpt-image-2-codex` currently **rejects** it |
| `moderation` | `low` | `low` / `auto` |
| `reference_images` | `[]` | array of local path / http(s) URL / `data:` URL / `{"data","mime"}` (image→image extension) |

Optional fields are only forwarded upstream when you set them, so existing calls behave exactly as before. If a batch (`n>1`) partially fails, the response carries the successful images plus a `warnings` array instead of failing wholesale.

## Configuration (environment variables)

| Variable | Default | Description |
|---|---|---|
| `CODEX_IMAGE_HOST` | `127.0.0.1` | listen address |
| `CODEX_IMAGE_PORT` | `10532` | listen port |
| `CODEX_IMAGE_MODEL` | `gpt-5.4-mini` | orchestration model driving the image_generation tool |
| `CODEX_IMAGE_CONCURRENCY` | `3` | max concurrent upstream generations |
| `CODEX_IMAGE_MAX_N` | `8` | per-request `n` cap |
| `CODEX_IMAGE_DIR` | `./generated` | output dir for `url` mode |
| `CODEX_IMAGE_AUTH_FILE` | *(auto)* | override `auth.json` path |
| `CODEX_IMAGE_PUBLIC_BASE` | `http://HOST:PORT` | address prefix returned in `url` mode |
| `CODEX_IMAGE_ALLOWED_HOSTS` | *(auto)* | extra allowed `Host` headers (comma-separated); only localhost by default — blocks DNS rebinding |
| `CODEX_IMAGE_MAX_BODY` | `67108864` | max request body in bytes (returns 413 if exceeded) |
| `CODEX_IMAGE_MAX_REFS` | `16` | max `reference_images` / uploaded images per request |
| `CODEX_IMAGE_TIMEOUT` | `400` | upstream socket timeout per generation attempt (seconds) |
| `CODEX_IMAGE_RETRIES` | `2` | extra attempts on transient upstream failures (network drop, 401/429/5xx, empty result) |

## Reliability notes

- **Set a generous client-side timeout.** `quality=high` takes 1–3 min per image; with `n>1` batches queue through `CODEX_IMAGE_CONCURRENCY` slots. If your HTTP client times out first you'll see the request "drop" while the server finishes (and logs `client disconnected before the response could be sent`). With the OpenAI SDK: `OpenAI(base_url=..., api_key="unused", timeout=600)`.
- **Retries are built in.** Transient upstream failures (SSE stream cut, 401 → token re-mint, 429/5xx, empty result) are retried with backoff before the request fails.
- **Disconnected clients stop burning quota.** If the caller hangs up while its job is queued or between retries, the server aborts that work.
- **One instance per port.** launchd KeepAlive, skill auto-start and `./run.sh` can race; the loser now exits cleanly instead of crash-looping on `Address already in use`.
- `GET /health` reports `active` / `queued` generation counts and `uptime_s` for quick triage.

## Project structure

```
.claude-plugin/   plugin + marketplace manifests (plugin.json / marketplace.json)
skills/           the two bundled skills: generate-image, connect-api
server.py         HTTP service: OpenAI-compatible routes + upstream Responses call + SSE parsing + token refresh
_codex_auth.py    auth.json discovery + JWT decode (zero side effects); shared by server.py and lib_preflight.py
lib_preflight.py  skill preflight: Codex login-state detection + server auto-start (single source of auto-start logic)
run.sh            control script: foreground / bg / stop / install-launchd
deploy/*.plist    macOS LaunchAgent template (placeholders filled on install)
examples/         curl + Python (OpenAI SDK) usage
```

> The skill scripts (`skills/*/`) locate the repo-root `lib_preflight.py` / `server.py` relative to `__file__`, so they find the backend wherever the plugin is installed; set `CODEX_IMAGE_SERVER_DIR` to point at a source checkout elsewhere.

## Autostart on boot (macOS launchd)

See the LaunchAgent template and install notes under [`deploy/`](./deploy/).

## Limitations

- Single-account generation is queued upstream, so `n>1` is close to serial in wall-clock time.
- `response_format=url` images land in local `generated/` and are only reachable on this machine (gitignored).
- Generation is slow (low ≈ 40s, high longer) — inherent overhead of going through the agent channel.
