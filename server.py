#!/usr/bin/env python3
"""
codex-image-api — a minimal, zero-dependency, OpenAI-compatible image API
backed by your ChatGPT / Codex subscription (no per-image API charge).

Flow:
    your code ──▶ POST http://127.0.0.1:PORT/v1/images/generations  (or /edits)
              ──▶ wrap the request as a Responses-API `image_generation` tool call
              ──▶ POST https://chatgpt.com/backend-api/codex/responses
                  (OAuth tokens read from ~/.codex/auth.json — same as Codex CLI)
              ──▶ parse the SSE stream, return OpenAI Images-shaped JSON.

Endpoints:
    POST /v1/images/generations   text->image (JSON). Supports n, response_format,
                                   and a `reference_images` extension (img->img).
    POST /v1/images/edits         image->image (multipart/form-data, standard
                                   OpenAI SDK `client.images.edit` entry point).
    GET  /images/<name>           serves images when response_format=url.
    GET  /health, GET /v1/models

Standard library only — no third-party packages, no telemetry; talks only to
chatgpt.com and auth.openai.com.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import _codex_auth  # type: ignore  # local: shared auth.json discovery + JWT decode

# --------------------------------------------------------------------------- #
# Config (all overridable via env)
# --------------------------------------------------------------------------- #
HOST = os.environ.get("CODEX_IMAGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_IMAGE_PORT", "10532"))
PUBLIC_BASE = os.environ.get("CODEX_IMAGE_PUBLIC_BASE", f"http://{HOST}:{PORT}")

UPSTREAM_BASE = "https://chatgpt.com/backend-api/codex"
OAUTH_ISSUER = "https://auth.openai.com"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Codex CLI's public client_id
ORCHESTRATION_MODEL = os.environ.get("CODEX_IMAGE_MODEL", "gpt-5.4-mini")

TIMEOUT = int(os.environ.get("CODEX_IMAGE_TIMEOUT", "400"))
MAX_CONCURRENCY = int(os.environ.get("CODEX_IMAGE_CONCURRENCY", "3"))
MAX_N = int(os.environ.get("CODEX_IMAGE_MAX_N", "8"))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BODY_BYTES = int(os.environ.get("CODEX_IMAGE_MAX_BODY", str(64 * 1024 * 1024)))
MAX_REF_IMAGES = int(os.environ.get("CODEX_IMAGE_MAX_REFS", "16"))

REFRESH_MARGIN = 5 * 60       # refresh if access token expires within 5 min
REFRESH_INTERVAL = 55 * 60    # ...or if last_refresh is older than 55 min

IMAGE_DIR = Path(os.environ.get(
    "CODEX_IMAGE_DIR", str(Path(__file__).resolve().parent / "generated")))


# Hosts we answer to. Rejecting other Host headers blocks DNS-rebinding: a web
# page you visit cannot make your browser drive this localhost API under an
# attacker-controlled hostname. Extend via CODEX_IMAGE_ALLOWED_HOSTS (comma list).
def _host_only(netloc_or_url: str) -> str:
    s = netloc_or_url.split("//", 1)[-1].split("/", 1)[0]
    if s.startswith("["):                       # IPv6 literal, e.g. [::1]:port
        return s[1:s.index("]")] if "]" in s else s
    return s.rsplit(":", 1)[0] if ":" in s else s


ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
if HOST and HOST != "0.0.0.0":
    ALLOWED_HOSTS.add(HOST)
ALLOWED_HOSTS.add(_host_only(PUBLIC_BASE))
ALLOWED_HOSTS.update(
    h.strip() for h in os.environ.get("CODEX_IMAGE_ALLOWED_HOSTS", "").split(",") if h.strip())

DEVELOPER_PROMPT = (
    "You are an image-generation assistant. Always invoke the image_generation "
    "tool. Pass the user's prompt through unchanged unless it is genuinely "
    "underspecified. Render at maximum technical quality for the chosen style. "
    "Do not add disclaimers."
)
DEVELOPER_PROMPT_WITH_REFS = (
    "You are an image-generation assistant. The user has provided one or more "
    "reference images. Inspect them, then invoke the image_generation tool to "
    "render a NEW image whose style, composition, palette, and mood are coherent "
    "with the references. Render at maximum technical quality. Do not add disclaimers."
)

VALID_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}
VALID_QUALITY = {"low", "medium", "high", "auto"}

EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

_auth_lock = threading.Lock()
_cached: dict | None = None
_gen_sem = threading.Semaphore(MAX_CONCURRENCY)


# --------------------------------------------------------------------------- #
# Auth: read ~/.codex/auth.json, decode JWT, refresh via OAuth when stale
# --------------------------------------------------------------------------- #
def _account_id_from_id_token(id_token: str | None) -> str | None:
    claims = _codex_auth.jwt_claims(id_token) or {}
    ns = claims.get("https://api.openai.com/auth")
    if isinstance(ns, dict):
        v = ns.get("chatgpt_account_id")
        if isinstance(v, str) and v:
            return v
    return None


def _parse_iso(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _read_auth_file() -> tuple[str, dict]:
    for p in _codex_auth.AUTH_CANDIDATES:
        if not p:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                return p, json.load(f)
        except Exception:
            continue
    raise RuntimeError(
        "auth.json not found. Run `codex login` once to mint it "
        f"(looked in: {[p for p in _codex_auth.AUTH_CANDIDATES if p]})."
    )


def _write_auth_file(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _refresh(refresh_token: str) -> dict:
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
        "scope": "openid profile email offline_access",
    }).encode()
    req = Request(OAUTH_ISSUER + "/oauth/token", data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def _stale(exp: float | None, last_refresh: str | None) -> bool:
    """True if the access token is near expiry, or last_refresh is too old."""
    now = time.time()
    if exp is not None and exp <= now + REFRESH_MARGIN:
        return True
    if last_refresh:
        ts = _parse_iso(last_refresh)
        if ts is not None and ts <= now - REFRESH_INTERVAL:
            return True
    return False


def get_auth() -> dict:
    """Return {access_token, account_id, exp}; refresh + persist if stale."""
    global _cached
    with _auth_lock:
        if _cached and not _stale(_cached["exp"], _cached["last_refresh"]):
            return _cached

        path, data = _read_auth_file()
        tokens = data.get("tokens") or {}
        access = tokens.get("access_token")
        id_token = tokens.get("id_token")
        refresh_token = tokens.get("refresh_token")
        account_id = tokens.get("account_id") or _account_id_from_id_token(id_token)
        last_refresh = data.get("last_refresh")

        if (not access or _stale(_codex_auth.jwt_exp(access), last_refresh)) and refresh_token:
            j = _refresh(refresh_token)
            access = j.get("access_token") or access
            id_token = j.get("id_token") or id_token
            refresh_token = j.get("refresh_token") or refresh_token
            account_id = _account_id_from_id_token(id_token) or account_id
            last_refresh = _iso_now()
            data["tokens"] = {
                "id_token": id_token, "access_token": access,
                "refresh_token": refresh_token, "account_id": account_id,
            }
            data["last_refresh"] = last_refresh
            data.setdefault("auth_mode", "chatgpt")
            _write_auth_file(path, data)

        if not access:
            raise RuntimeError("no access_token available (refresh failed?)")
        if not account_id:
            raise RuntimeError("account_id not found in tokens / id_token claims")

        _cached = {"access_token": access, "account_id": account_id,
                   "exp": _codex_auth.jwt_exp(access), "last_refresh": last_refresh}
        return _cached


# --------------------------------------------------------------------------- #
# Reference images (img->img): build `input_image` content parts
# --------------------------------------------------------------------------- #
def _looks_like_image(buf: bytes) -> bool:
    """True only if buf starts with a known image magic number."""
    return (buf[:4] == b"\x89PNG"
            or buf[:3] == b"\xff\xd8\xff"
            or (buf[:4] == b"RIFF" and buf[8:12] == b"WEBP")
            or buf[:6] in (b"GIF87a", b"GIF89a")
            or buf[:2] == b"BM")


def _sniff_image_mime(buf: bytes) -> str:
    if buf[:4] == b"\x89PNG":
        return "image/png"
    if buf[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "image/webp"
    if buf[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def _forbid_auth_dir(resolved: str) -> None:
    for cand in _codex_auth.AUTH_CANDIDATES:
        if cand and os.path.commonpath([resolved, os.path.dirname(os.path.realpath(cand))])\
                == os.path.dirname(os.path.realpath(cand)):
            raise ValueError("reference path points into the auth-file area; refused")


def _image_part_from_bytes(data: bytes, mime: str | None) -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"reference image exceeds {MAX_UPLOAD_BYTES} bytes")
    m = mime if (mime and mime.startswith("image/")) else _sniff_image_mime(data)
    b64 = base64.b64encode(data).decode()
    return {"type": "input_image", "image_url": f"data:{m};base64,{b64}"}


def _resolve_reference(spec) -> dict:
    """Turn a reference_images entry into an input_image content part."""
    if isinstance(spec, str):
        if spec.startswith("data:") or spec.startswith(("http://", "https://")):
            return {"type": "input_image", "image_url": spec}  # upstream resolves
        resolved = os.path.realpath(spec)
        _forbid_auth_dir(resolved)
        if not os.path.isfile(resolved):
            raise ValueError(f"reference image not found: {spec}")
        with open(resolved, "rb") as f:
            data = f.read(MAX_UPLOAD_BYTES + 1)  # bound memory; size re-checked below
        if not _looks_like_image(data):  # don't ship arbitrary local files upstream
            raise ValueError("reference path is not a recognized image; refused")
        return _image_part_from_bytes(data, EXT_MIME.get(Path(resolved).suffix.lower()))
    if isinstance(spec, dict) and spec.get("data"):
        mime = spec.get("mime", "image/png")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{spec['data']}"}
    raise ValueError("invalid reference_images entry (want path/url/data-url/{data,mime})")


# --------------------------------------------------------------------------- #
# Upstream call + SSE parsing + image generation (with batching/concurrency)
# --------------------------------------------------------------------------- #
def _build_body(prompt: str, ref_parts: list[dict], size: str,
                quality: str, moderation: str) -> dict:
    has_refs = bool(ref_parts)
    if has_refs:
        content = list(ref_parts)
        content.append({"type": "input_text", "text": f"Generate an image: {prompt}"})
        user_msg = {"role": "user", "content": content}
    else:
        user_msg = {"role": "user", "content": f"Generate an image: {prompt}"}
    return {
        "model": ORCHESTRATION_MODEL,
        "input": [
            {"role": "developer",
             "content": DEVELOPER_PROMPT_WITH_REFS if has_refs else DEVELOPER_PROMPT},
            user_msg,
        ],
        "tools": [{"type": "image_generation", "quality": quality,
                   "size": size, "moderation": moderation}],
        "tool_choice": "auto" if has_refs else "required",
        "reasoning": {"effort": "low"},
        "stream": True,
        "store": False,
        "instructions": "",
    }


def _iter_sse(resp):
    data_lines: list[str] = []

    def flush():
        if not data_lines:
            return None
        payload = "".join(data_lines)
        data_lines.clear()
        if payload and payload != "[DONE]":
            try:
                return json.loads(payload)
            except Exception:
                return None
        return None

    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            ev = flush()
            if ev is not None:
                yield ev
            continue
        if line.startswith("data: "):
            data_lines.append(line[6:])
        elif line.startswith("data:"):
            data_lines.append(line[5:])
    ev = flush()
    if ev is not None:
        yield ev


def _generate_one(prompt: str, ref_parts: list[dict], size: str,
                  quality: str, moderation: str) -> tuple[str, str | None]:
    auth = get_auth()
    headers = {
        "Authorization": f"Bearer {auth['access_token']}",
        "chatgpt-account-id": auth["account_id"],
        "OpenAI-Beta": "responses=experimental",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = json.dumps(_build_body(prompt, ref_parts, size, quality, moderation)).encode()
    req = Request(UPSTREAM_BASE + "/responses", data=body, headers=headers, method="POST")
    b64 = revised = None
    events = 0
    with _gen_sem:  # cap concurrent upstream calls to avoid rate limiting
        with urlopen(req, timeout=TIMEOUT) as resp:
            for ev in _iter_sse(resp):
                events += 1
                t = ev.get("type")
                if t == "response.output_item.done":
                    item = ev.get("item") or {}
                    if item.get("type") == "image_generation_call":
                        result = item.get("result")
                        if isinstance(result, str) and result:
                            b64 = result
                        rp = item.get("revised_prompt")
                        if isinstance(rp, str):
                            revised = rp
                elif t == "error":
                    raise RuntimeError(f"upstream error event: {json.dumps(ev)[:200]}")
    if not b64:
        raise RuntimeError(f"no image data after {events} stream events")
    return b64, revised


def generate_images(prompt: str, ref_parts: list[dict], size: str,
                    quality: str, moderation: str, n: int) -> list[tuple[str, str | None]]:
    if n <= 1:
        return [_generate_one(prompt, ref_parts, size, quality, moderation)]
    workers = min(n, MAX_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_generate_one, prompt, ref_parts, size, quality, moderation)
                for _ in range(n)]
        return [f.result() for f in futs]


# --------------------------------------------------------------------------- #
# Output formatting (b64_json | url)
# --------------------------------------------------------------------------- #
def _save_image(b64: str) -> str:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    img = base64.b64decode(b64)
    name = f"{int(time.time())}-{hashlib.sha1(img).hexdigest()[:12]}.png"
    (IMAGE_DIR / name).write_bytes(img)
    return f"{PUBLIC_BASE}/images/{name}"


def _to_data_items(results: list[tuple[str, str | None]], response_format: str) -> list[dict]:
    items = []
    for b64, revised in results:
        item = {"url": _save_image(b64)} if response_format == "url" else {"b64_json": b64}
        if revised:
            item["revised_prompt"] = revised
        items.append(item)
    return items


# --------------------------------------------------------------------------- #
# multipart/form-data parsing (binary-safe, zero-dependency)
# --------------------------------------------------------------------------- #
def _disp_param(disp: str, key: str) -> str | None:
    m = re.search(rf'{key}="([^"]*)"', disp)
    return m.group(1) if m else None


def _parse_multipart(body: bytes, boundary: str) -> list[dict]:
    sep = b"\r\n--" + boundary.encode()
    segs = (b"\r\n" + body).split(sep)
    parts = []
    for seg in segs:
        if seg in (b"", b"--", b"--\r\n") or seg.startswith(b"--"):
            continue
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        if b"\r\n\r\n" not in seg:
            continue
        raw_headers, data = seg.split(b"\r\n\r\n", 1)
        headers = {}
        for line in raw_headers.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
        disp = headers.get("content-disposition", "")
        parts.append({
            "name": _disp_param(disp, "name"),
            "filename": _disp_param(disp, "filename"),
            "content_type": headers.get("content-type"),
            "data": data,
        })
    return parts


def _coerce_n(raw) -> int:
    """Parse n; return a sentinel that _common rejects cleanly on bad input."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _extract_params(m: dict) -> dict:
    """Pull the shared image params (with defaults) from a JSON / form mapping."""
    return {
        "prompt": m.get("prompt"),
        "size": m.get("size") or "1024x1024",
        "quality": m.get("quality") or "high",
        "moderation": m.get("moderation") or "low",
        "n": _coerce_n(m.get("n") or 1),
        "response_format": m.get("response_format") or "b64_json",
    }


# --------------------------------------------------------------------------- #
# HTTP server (OpenAI-compatible surface)
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "codex-image-api/0.3"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------- #
    def _send_json(self, code: int, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_error(self, code: int, msg: str, typ: str = "invalid_request_error") -> None:
        self._send_json(code, {"error": {"message": msg, "type": typ}})

    def _read_body(self) -> bytes | None:
        """Read the body, or send 413 + return None if it exceeds the cap."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # don't desync the keep-alive stream
            self._send_error(413, f"request body exceeds {MAX_BODY_BYTES} bytes",
                             "payload_too_large")
            return None
        return self.rfile.read(length) if length else b""

    def _host_ok(self) -> bool:
        """Reject Host headers we don't recognize (anti DNS-rebinding)."""
        host = self.headers.get("Host", "")
        name = (host[1:host.index("]")] if host.startswith("[") and "]" in host
                else host.rsplit(":", 1)[0] if ":" in host else host)
        return name in ALLOWED_HOSTS or host in ALLOWED_HOSTS

    def log_message(self, format, *args):  # noqa: A002 (match base signature)
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _common(self, prompt, ref_parts, size, quality, moderation, n, response_format):
        """Shared generation + response path for generations and edits."""
        if not isinstance(prompt, str) or not prompt.strip():
            return self._send_error(400, "`prompt` is required and must be a non-empty string")
        if size not in VALID_SIZES:
            return self._send_error(400, f"invalid size {size!r}; allowed: {sorted(VALID_SIZES)}")
        if quality not in VALID_QUALITY:
            return self._send_error(400, f"invalid quality {quality!r}")
        if not (1 <= n <= MAX_N):
            return self._send_error(400, f"`n` must be between 1 and {MAX_N}")
        if response_format not in ("b64_json", "url"):
            return self._send_error(400, "`response_format` must be 'b64_json' or 'url'")
        try:
            results = generate_images(prompt, ref_parts, size, quality, moderation, n)
        except HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return self._send_error(502, f"upstream HTTP {e.code}: {detail}", "upstream_error")
        except URLError as e:
            return self._send_error(502, f"network error: {e.reason}", "upstream_error")
        except Exception as e:
            return self._send_error(502, f"generation failed: {e}", "upstream_error")
        self._send_json(200, {
            "created": int(time.time()),
            "data": _to_data_items(results, response_format),
            "usage": {},
        })

    # -- routes ----------------------------------------------------------- #
    def do_GET(self):
        if not self._host_ok():
            return self._send_error(403, "host not allowed", "forbidden")
        if self.path == "/health":
            try:
                a = get_auth()
                exp = a.get("exp")
                detail = f"expires in {int(exp - time.time())}s" if exp else "loaded"
                self._send_json(200, {"ok": True, "auth": detail,
                                      "model": ORCHESTRATION_MODEL,
                                      "concurrency": MAX_CONCURRENCY, "version": "0.3"})
            except Exception as e:
                self._send_json(200, {"ok": False, "auth": str(e)})
            return
        if self.path == "/v1/models":
            self._send_json(200, {"object": "list", "data": [
                {"id": "gpt-image-2", "object": "model", "owned_by": "openai"}]})
            return
        if self.path.startswith("/images/"):
            return self._serve_image(self.path[len("/images/"):])
        self._send_error(404, f"not found: {self.path}", "not_found")

    def _serve_image(self, name: str):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):  # no path traversal
            return self._send_error(404, "not found", "not_found")
        fpath = IMAGE_DIR / name
        if not fpath.is_file():
            return self._send_error(404, "image not found", "not_found")
        data = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._host_ok():
            return self._send_error(403, "host not allowed", "forbidden")
        route = self.path.rstrip("/")
        if route == "/v1/images/generations":
            return self._handle_generations()
        if route == "/v1/images/edits":
            return self._handle_edits()
        self._send_error(404, f"not found: {self.path}", "not_found")

    def _handle_generations(self):
        body = self._read_body()
        if body is None:
            return  # 413 already sent
        try:
            req = json.loads(body or b"{}")
        except Exception:
            return self._send_error(400, "invalid JSON body")
        refs = req.get("reference_images") or []
        if len(refs) > MAX_REF_IMAGES:
            return self._send_error(400, f"too many reference_images (max {MAX_REF_IMAGES})")
        try:
            ref_parts = [_resolve_reference(s) for s in refs]
        except Exception as e:
            return self._send_error(400, f"bad reference_images: {e}")
        self._common(ref_parts=ref_parts, **_extract_params(req))

    def _handle_edits(self):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r"boundary=([^;]+)", ctype)
        if "multipart/form-data" not in ctype or not m:
            return self._send_error(400, "expected multipart/form-data with a boundary")
        boundary = m.group(1).strip().strip('"')
        body = self._read_body()
        if body is None:
            return  # 413 already sent
        parts = _parse_multipart(body, boundary)
        fields: dict[str, str] = {}
        ref_parts: list[dict] = []
        try:
            for p in parts:
                if p["filename"] is not None or (p["name"] or "").startswith("image"):
                    if p["data"]:
                        ref_parts.append(_image_part_from_bytes(p["data"], p["content_type"]))
                elif p["name"]:
                    fields[p["name"]] = p["data"].decode("utf-8", "replace")
        except Exception as e:
            return self._send_error(400, f"bad image upload: {e}")
        if not ref_parts:
            return self._send_error(400, "at least one `image` file part is required")
        if len(ref_parts) > MAX_REF_IMAGES:
            return self._send_error(400, f"too many image parts (max {MAX_REF_IMAGES})")
        self._common(ref_parts=ref_parts, **_extract_params(fields))


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"codex-image-api listening on http://{HOST}:{PORT}  (concurrency={MAX_CONCURRENCY})")
    print("  POST /v1/images/generations   text->image (JSON; n, response_format, reference_images)")
    print("  POST /v1/images/edits         image->image (multipart, OpenAI SDK compatible)")
    print("  GET  /images/<name>           serves url-mode images")
    print("  GET  /health · /v1/models")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
