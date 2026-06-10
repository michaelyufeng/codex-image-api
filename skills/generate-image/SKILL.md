---
name: generate-image
description: 生成或编辑图片（走本地 codex-image-api，复用 ChatGPT 订阅额度的 gpt-image-2，不花 API 费）。当用户想画图/生成图片/做插画/图标/海报/封面，或想修改、编辑一张已有图片时使用。触发词：画一张、生成图片、做张图、来张图、把这张图改成、生成一张配图、generate an image、draw、create a picture、edit this image。
---

# generate-image

用本地常驻的 **codex-image-api** 生图，复用 Codex / ChatGPT 订阅额度（gpt-image-2），不走付费 API。
脚本会**自动确保 API 在运行**（不在线就拉起，约 2 秒），你无需手动启动服务。

## 用法

文生图：
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/generate-image/generate.py" "赛博朋克风格的猫，霓虹灯光" -s 1024x1024 -q high
```

图生图（带一张或多张参考图）：
```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/generate-image/generate.py" "把围巾改成蓝色" -i /path/to/input.png
```

参数：
- 位置参数 `prompt`：图片描述（必填）
- `-i/--image`：参考图路径，触发图生图，可重复传多张（最多 16 张）
- `-s/--size`：`1024x1024`(默认) / `1024x1536` / `1536x1024` / `auto`
- `-q/--quality`：`low` / `medium` / `high`(默认) / `auto`
- `-n/--count`：生成数量（默认 1）
- `-o/--out`：输出路径（默认 `./image-<时间戳>.png`）
- `--format`：输出格式 `png`(默认) / `jpeg` / `webp`
- `--compression`：jpeg/webp 压缩率 0–100
- `--timeout`：HTTP 超时秒数（默认按 `-n` 自动放大，一般不用手动传）

> 上游 gpt-image-2-codex 目前**不支持**透明背景（`background=transparent`）和 `input_fidelity`，所以脚本不提供这两个选项；用户要透明底时直接说明做不到、建议生成纯色底后自行抠图。

脚本把图片存到本地，并在 **stdout 每行打印一个绝对路径**；进度信息走 stderr。

## 给 Claude 的执行指引

1. 用户想生图/改图时，调用上面的脚本，把用户的描述当作 `prompt`。
2. 改一张已有图片时加 `-i <图片路径>`（用户提供的图）。
3. 脚本输出图片绝对路径后，用 `imgcat <路径>` 把图显示给用户。
4. 首次调用若服务没起，脚本会自动拉起（约 2 秒），**不要**自己手动去启动 server.py。
5. 质量默认 `high`（较慢，约 1–3 分钟）；用户要快就加 `-q low`。服务端对上游瞬断/限流会自动重试，慢不等于挂了，**耐心等脚本退出**。
6. 脚本启动时会先做**登录验证**：未登录会自动运行 `codex login`（弹浏览器授权，登完自动继续）；未安装 Codex 会提示 `npm install -g @openai/codex`。失败时脚本非零退出并在 stderr 打印原因。
