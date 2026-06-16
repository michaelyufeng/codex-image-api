#!/usr/bin/env python3
"""generate-image skill — text->image / image->image via local codex-image-api.

Verifies Codex login (auto-guides if needed), ensures the API is running, then
generates, saves to disk, and prints absolute path(s) to stdout. Stderr = progress.
Login check + server auto-start are shared with the connect-api skill via
`lib_preflight` (lives in the codex2image project). Zero third-party dependencies.
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Shared preflight (login check + server auto-start) lives in the plugin root.
# This script sits at <root>/skills/generate-image/generate.py, so the backend
# (lib_preflight.py, server.py) is two levels up. Resolving relative to __file__
# keeps it working wherever the plugin is installed; CODEX_IMAGE_SERVER_DIR
# overrides for a dev checkout elsewhere.
_BACKEND = os.environ.get("CODEX_IMAGE_SERVER_DIR") or str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, _BACKEND)
import lib_preflight as preflight  # type: ignore  # noqa: E402

API = preflight.SERVER_URL


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _do(req: urllib.request.Request, timeout: int = 480) -> dict:
    """Issue the request; turn server 4xx/5xx and network errors into a clean
    fatal message instead of an uncaught traceback. The server always pairs an
    error with an HTTP error status (403/413/400/502...), so urlopen raises
    HTTPError rather than returning an {"error": ...} body — that's the real
    error channel we must handle here."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = (json.loads(body).get("error") or {}).get("message") or body
        except Exception:
            msg = body
        sys.exit(f"生成失败: HTTP {e.code} — {msg[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"生成失败: 无法连接本地 API（{e.reason}）；详见 /tmp/codex-image-api.log")


def _post_json(path: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return _do(req, timeout)


def _post_multipart(path: str, fields: dict, files: list[tuple[str, str]],
                    timeout: int) -> dict:
    boundary = "----codeximg" + str(int(time.time() * 1000))
    body = b""
    for k, v in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"'
                 f"\r\n\r\n{v}\r\n").encode()
    for field, fpath in files:
        fn = os.path.basename(fpath)
        with open(fpath, "rb") as f:
            content = f.read()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                 f'filename="{fn}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        API + path, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    return _do(req, timeout)


def main() -> None:
    preflight.require_auth(on_not_logged_in="auto_login")   # 登录验证前置（未登录自动引导）

    ap = argparse.ArgumentParser(description="Generate or edit images via codex-image-api")
    ap.add_argument("prompt", help="image description")
    ap.add_argument("-i", "--image", action="append", default=[], help="reference image(s) -> edit")
    ap.add_argument("-s", "--size", default="1024x1024",
                    choices=["1024x1024", "1024x1536", "1536x1024", "auto"])
    ap.add_argument("-q", "--quality", default="high",
                    choices=["low", "medium", "high", "auto"])
    ap.add_argument("-n", "--count", type=int, default=1)
    ap.add_argument("-o", "--out", help="output path (default ./image-<ts>.png)")
    # NOTE: upstream gpt-image-2-codex currently rejects `background=transparent`
    # and `input_fidelity`, so those are deliberately NOT exposed here. The
    # server still passes them through for clients that want to probe support.
    ap.add_argument("--format", dest="output_format", choices=["png", "jpeg", "webp"],
                    help="output image format (default png)")
    ap.add_argument("--compression", type=int,
                    help="jpeg/webp compression level 0-100")
    ap.add_argument("--timeout", type=int,
                    help="HTTP timeout seconds (default scales with -n)")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium",
                    help="reasoning effort. default medium = model thinks + expands a "
                         "photographic brief before drawing (more realistic, slower); "
                         "high = deepest thinking; low = fast passthrough (no expansion).")
    a = ap.parse_args()

    preflight.ensure_server_running()                       # 统一自启（共享逻辑）
    _log(f"生成中（{'图生图' if a.image else '文生图'}, quality={a.quality}, "
         f"n={a.count}, effort={a.effort}）…")

    # high 质量单张可达 2-3 分钟；n 张按服务端并发 3 分批 → 超时按批数放大。
    # deep（effort != low）先思考再画，单批更慢 → 把每批基数也放大。
    _per_batch = 720 if a.effort != "low" else 480
    timeout = a.timeout or (_per_batch * -(-a.count // 3))

    extra = {k: v for k, v in {
        "output_format": a.output_format,
        "output_compression": a.compression,
    }.items() if v is not None}

    if a.image:
        for p in a.image:
            if not os.path.isfile(p):
                sys.exit(f"参考图不存在: {p}")
        fields = {"prompt": a.prompt, "size": a.size,
                  "quality": a.quality, "n": str(a.count), "effort": a.effort}
        fields.update({k: str(v) for k, v in extra.items()})
        resp = _post_multipart("/v1/images/edits", fields,
                               [("image", p) for p in a.image], timeout)
    else:
        payload = {"prompt": a.prompt, "size": a.size,
                   "quality": a.quality, "n": a.count, "effort": a.effort}
        payload.update(extra)
        resp = _post_json("/v1/images/generations", payload, timeout)

    for w in resp.get("warnings") or []:
        _log(f"⚠️  {w}")
    data = resp.get("data") or []
    ext = {"jpeg": ".jpg", "webp": ".webp"}.get(a.output_format or "png", ".png")
    stem = (a.out.rsplit(".", 1)[0] if a.out else f"image-{int(time.time())}")
    for i, item in enumerate(data):
        out = (a.out if (a.out and len(data) == 1)
               else f"{stem}{ext}" if len(data) == 1 else f"{stem}-{i + 1}{ext}")
        Path(out).write_bytes(base64.b64decode(item["b64_json"]))
        brief = item.get("revised_prompt")
        if a.effort != "low" and brief:    # deep: 存扩写后的摄影 brief,验证思考真生效
            Path(out + ".brief.txt").write_text(brief, encoding="utf-8")
        print(os.path.abspath(out))  # stdout: one path per line


if __name__ == "__main__":
    main()
