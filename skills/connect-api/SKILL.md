---
name: connect-api
description: 把一个现有项目从 OpenAI 官方 API 一键切到本地 codex-image-api（复用 ChatGPT 订阅额度的 gpt-image-2，不花 API 费）。当用户想把某个项目接入本地生图 API、切到 codex-image-api、用订阅额度替换官方 OpenAI key 省钱、或迁移项目的生图后端时使用。触发词：接入本地API、切到本地生图、换成codex生图省钱、把这个项目接到本地API、connect a project to the local image API、migrate to local API。
---

# connect-api

把一个用 OpenAI SDK 生图的现有项目，切到本地 **codex-image-api**——**通常零改代码**，靠给项目 `.env` 写入 `OPENAI_BASE_URL` 实现。脚本会先验证 Codex 登录、确保后端在线、幂等改配置、探测连通，并对已知项目（如示例）给出成本/预算提示。

## 用法

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/connect-api/connect.py" "<项目路径>"
# 例：
python3 "${CLAUDE_PLUGIN_ROOT}/skills/connect-api/connect.py" "/Users/user/Claude code/example"
```

## 给 Claude 的执行指引

1. 用户想把某个项目接入本地 API 时，用**项目根目录路径**调用上面的脚本。
2. 脚本依次做：①验证 Codex 登录（未登录会提示运行 `codex login`，不自动弹浏览器）②确保本地 server 在线（不在线自动拉起）③给项目 `.env` 幂等写入 `OPENAI_BASE_URL=http://127.0.0.1:10532/v1` ④探测连通 ⑤打印报告与提示。
3. **前提**：目标项目用 OpenAI SDK 且**未硬编码 base_url**（绝大多数项目靠环境变量即可，零改码）。若项目硬编码了 base_url，需手动改代码。
4. 接入后，建议触发该项目一次真实生图，确认确实走本地 API（查看 `/tmp/codex-image-api.log` 是否有请求）。
5. 接入是配置层面的改动（改 `.env`），原 `OPENAI_API_KEY` 保留即可——本地 server 会忽略它的值。
6. 想让后端开机常驻，提示用户运行 `"${CLAUDE_PLUGIN_ROOT}/run.sh" install-launchd`（或在 codex2image 源码目录里 `./run.sh install-launchd`）。
