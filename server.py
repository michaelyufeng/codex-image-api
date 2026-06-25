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
import errno
import hashlib
import http.client
import json
import os
import re
import select
import socket
import sys
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
ORCHESTRATION_MODEL = os.environ.get("CODEX_IMAGE_MODEL", "gpt-5.5")


def _plugin_version() -> str:
    """Single source of truth for the version: read it from plugin.json so a
    release only has to bump that one file — /health, the banner and the Server
    header all follow automatically."""
    try:
        p = Path(__file__).resolve().parent / ".claude-plugin" / "plugin.json"
        return json.loads(p.read_text(encoding="utf-8")).get("version", "0")
    except Exception:
        return "0"


__version__ = _plugin_version()

TIMEOUT = int(os.environ.get("CODEX_IMAGE_TIMEOUT", "400"))
MAX_CONCURRENCY = int(os.environ.get("CODEX_IMAGE_CONCURRENCY", "3"))
RETRIES = int(os.environ.get("CODEX_IMAGE_RETRIES", "2"))  # extra attempts on transient upstream failure
RETRY_BACKOFF = 3.0                                        # seconds; doubles per attempt
MAX_N = int(os.environ.get("CODEX_IMAGE_MAX_N", "8"))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BODY_BYTES = int(os.environ.get("CODEX_IMAGE_MAX_BODY", str(64 * 1024 * 1024)))
MAX_REF_IMAGES = int(os.environ.get("CODEX_IMAGE_MAX_REFS", "16"))
# Forwarding a caller-supplied http(s) reference URL makes the UPSTREAM fetch it
# server-side (SSRF / confused-deputy). Off by default; opt in only for trusted callers.
ALLOW_REMOTE_REFS = os.environ.get("CODEX_IMAGE_ALLOW_REMOTE_REFS", "").lower() in ("1", "true", "yes")

REFRESH_MARGIN = 5 * 60       # refresh if access token expires within 5 min
REFRESH_INTERVAL = 55 * 60    # fallback (exp unreadable): refresh if last_refresh older than this

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

# Deep mode (reasoning.effort != "low"): let the model think first and expand a
# SHORT user intent into a precise photographic brief before drawing. Trades
# ~3-4x latency for markedly more realistic, less "AI-looking" output. Kept
# general on purpose — project-specific direction (composition, palette, mood)
# is supplied by the caller's prompt, not baked in here.
DEVELOPER_PROMPT_DEEP = (
    "You are a senior photography art director and the image engine. Think "
    "carefully before generating: the user gives a SHORT intent — silently expand "
    "it into a precise photographic brief, then invoke the image_generation tool "
    "exactly once. Before rendering, lock down (1) lighting: natural directional "
    "light with soft falloff, never flat AI lighting; (2) lens & framing: a "
    "concrete focal length, camera height and crop; (3) skin/material: real "
    "visible texture and a warm natural tone, no plastic beauty-smoothing — warm "
    "reads as a real photo, cold 'clean' tones read as AI; (4) composition and "
    "pose. Express everything as CONCRETE POSITIVE description — the renderer "
    "ignores abstract negations like 'not blurry', so state the positive look "
    "instead. Render at maximum technical quality. No disclaimers, and no text "
    "overlays unless the user explicitly asks for them."
)
DEVELOPER_PROMPT_DEEP_WITH_REFS = (
    "The user provided one or more reference images. Inspect them first and "
    "preserve the identity, the garment's color/cut/print, and the scene "
    "faithfully; do not invent products. " + DEVELOPER_PROMPT_DEEP
)

VALID_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}
VALID_QUALITY = {"low", "medium", "high", "auto"}
VALID_MODERATION = {"low", "auto"}
VALID_BACKGROUND = {"transparent", "opaque", "auto"}
VALID_OUTPUT_FORMAT = {"png", "jpeg", "webp"}
VALID_FIDELITY = {"low", "high"}
VALID_EFFORT = {"low", "medium", "high"}

EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}
FORMAT_EXT = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}

_auth_lock = threading.Lock()
_cached: dict | None = None
_gen_sem = threading.Semaphore(MAX_CONCURRENCY)
_started_at = time.time()
_stats_lock = threading.Lock()
_stats = {"active": 0, "queued": 0}
_peek_lock = threading.Lock()  # serialize MSG_PEEK on a shared client socket


def _stat(key: str, delta: int) -> None:
    with _stats_lock:
        _stats[key] += delta


class ClientDisconnected(Exception):
    """The HTTP client hung up while its generation was queued/running."""


class UpstreamError(RuntimeError):
    """Upstream replied with an HTTP error status."""

    def __init__(self, code: int, detail: str, retry_after: float | None = None):
        super().__init__(f"upstream HTTP {code}: {detail}")
        self.code = code
        self.retry_after = retry_after


def _client_gone(sock) -> bool:
    """Best-effort: True if the client already closed its side of the socket.

    Serialized via _peek_lock: for n>1 batches the same connection socket is
    polled from multiple worker threads, and concurrent select()+MSG_PEEK recv
    on one socket is a data race that can spuriously report a disconnect.
    """
    with _peek_lock:
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            if not readable:
                return False       # nothing to read -> connection still open
            return sock.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True


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
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:           # assume UTC for naive stamps (auth.json is shared)
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
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
    """True if the access token is near expiry.

    When exp is readable, it alone decides — access tokens live for days, and
    refreshing on a wall-clock interval would needlessly rotate the
    refresh_token, racing Codex CLI's own copy of auth.json (a 401 source).
    The interval check only kicks in as a fallback when exp can't be read.
    """
    now = time.time()
    if exp is not None:
        return exp <= now + REFRESH_MARGIN
    if last_refresh:
        ts = _parse_iso(last_refresh)
        if ts is not None and ts <= now - REFRESH_INTERVAL:
            return True
    return False


def get_auth(force_refresh: bool = False) -> dict:
    """Return {access_token, account_id, exp}; refresh + persist if stale.

    force_refresh: bypass the cache and rotate via refresh_token now — used
    after an upstream 401, where the token is bad despite a future exp.
    """
    global _cached
    with _auth_lock:
        if _cached and not force_refresh and not _stale(_cached["exp"], _cached["last_refresh"]):
            return _cached

        path, data = _read_auth_file()
        tokens = data.get("tokens") or {}
        access = tokens.get("access_token")
        id_token = tokens.get("id_token")
        refresh_token = tokens.get("refresh_token")
        account_id = tokens.get("account_id") or _account_id_from_id_token(id_token)
        last_refresh = data.get("last_refresh")

        needs_refresh = (force_refresh or not access
                         or _stale(_codex_auth.jwt_exp(access), last_refresh))
        if needs_refresh and refresh_token:
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
        elif needs_refresh and not access:
            # need a token but can't refresh (no refresh_token) and have no usable access
            raise RuntimeError("auth.json has no usable access_token and no "
                               "refresh_token; run `codex login` to re-authenticate")

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
def _sniff_image_mime(buf: bytes) -> str | None:
    """Image MIME from magic bytes, or None if buf isn't a recognized image.
    Single source of truth for image detection (see _looks_like_image)."""
    if buf[:4] == b"\x89PNG":
        return "image/png"
    if buf[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP":
        return "image/webp"
    if buf[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if buf[:2] == b"BM":
        return "image/bmp"
    return None


def _looks_like_image(buf: bytes) -> bool:
    """True only if buf starts with a known image magic number."""
    return _sniff_image_mime(buf) is not None


def _forbid_auth_dir(resolved: str) -> None:
    for cand in _codex_auth.AUTH_CANDIDATES:
        if cand and os.path.commonpath([resolved, os.path.dirname(os.path.realpath(cand))])\
                == os.path.dirname(os.path.realpath(cand)):
            raise ValueError("reference path points into the auth-file area; refused")


def _image_part_from_bytes(data: bytes, mime: str | None) -> dict:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"reference image exceeds {MAX_UPLOAD_BYTES} bytes")
    m = mime if (mime and mime.startswith("image/")) else (_sniff_image_mime(data) or "image/png")
    b64 = base64.b64encode(data).decode()
    return {"type": "input_image", "image_url": f"data:{m};base64,{b64}"}


def _resolve_reference(spec) -> dict:
    """Turn a reference_images entry into an input_image content part."""
    if isinstance(spec, str):
        if spec.startswith("data:"):
            return {"type": "input_image", "image_url": spec}
        if spec.startswith(("http://", "https://")):
            if not ALLOW_REMOTE_REFS:  # SSRF guard — upstream would fetch it server-side
                raise ValueError(
                    "remote URL reference images are disabled; set "
                    "CODEX_IMAGE_ALLOW_REMOTE_REFS=1 to allow, or pass a local "
                    "path / data: URL instead")
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
        if not (isinstance(mime, str) and mime.startswith("image/")):
            raise ValueError(f"invalid reference mime {mime!r}; must start with 'image/'")
        return {"type": "input_image", "image_url": f"data:{mime};base64,{spec['data']}"}
    raise ValueError("invalid reference_images entry (want path/url/data-url/{data,mime})")


# --------------------------------------------------------------------------- #
# Upstream call + SSE parsing + image generation (with batching/concurrency)
# --------------------------------------------------------------------------- #
def _build_body(prompt: str, ref_parts: list[dict], size: str,
                quality: str, moderation: str, tool_extra: dict,
                effort: str = "low") -> dict:
    has_refs = bool(ref_parts)
    deep = effort != "low"   # deep: think + expand a brief before drawing
    if has_refs:
        content = list(ref_parts)
        content.append({"type": "input_text", "text": f"Generate an image: {prompt}"})
        user_msg = {"role": "user", "content": content}
    else:
        user_msg = {"role": "user", "content": f"Generate an image: {prompt}"}
    tool = {"type": "image_generation", "quality": quality,
            "size": size, "moderation": moderation}
    tool.update(tool_extra)  # background / output_format / output_compression / input_fidelity
    if has_refs:
        developer = DEVELOPER_PROMPT_DEEP_WITH_REFS if deep else DEVELOPER_PROMPT_WITH_REFS
    else:
        developer = DEVELOPER_PROMPT_DEEP if deep else DEVELOPER_PROMPT
    return {
        "model": ORCHESTRATION_MODEL,
        "input": [
            {"role": "developer", "content": developer},
            user_msg,
        ],
        "tools": [tool],
        # force the tool in deep mode too, else effort=high may "think but not draw"
        "tool_choice": "required" if (deep or not has_refs) else "auto",
        "reasoning": {"effort": effort},
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


def _attempt_generate(prompt: str, ref_parts: list[dict], size: str, quality: str,
                      moderation: str, tool_extra: dict,
                      effort: str = "low") -> tuple[str, str | None]:
    """One upstream call. Raises UpstreamError on HTTP errors, RuntimeError on
    empty results, raw OSError/HTTPException on transport failures."""
    auth = get_auth()
    headers = {
        "Authorization": f"Bearer {auth['access_token']}",
        "chatgpt-account-id": auth["account_id"],
        "OpenAI-Beta": "responses=experimental",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = json.dumps(_build_body(prompt, ref_parts, size, quality,
                                  moderation, tool_extra, effort)).encode()
    req = Request(UPSTREAM_BASE + "/responses", data=body, headers=headers, method="POST")
    b64 = revised = None
    events = 0
    text_parts: list[str] = []
    # deep (effort != low) thinks + expands → needs longer than the low fast path
    to = max(TIMEOUT, 720) if effort != "low" else TIMEOUT
    try:
        with urlopen(req, timeout=to) as resp:
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
                    elif item.get("type") == "message":
                        for part in item.get("content") or []:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                text_parts.append(part["text"])
                elif t == "error":
                    raise RuntimeError(f"upstream error event: {json.dumps(ev)[:200]}")
    except HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        try:
            retry_after = float(e.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            retry_after = None
        raise UpstreamError(e.code, detail, retry_after) from None
    if not b64:
        said = f"; assistant said: {' '.join(text_parts)[:200]}" if text_parts else ""
        raise RuntimeError(f"no image data after {events} stream events{said}")
    return b64, revised


def _generate_one(prompt: str, ref_parts: list[dict], size: str, quality: str,
                  moderation: str, tool_extra: dict, alive=None,
                  effort: str = "low") -> tuple[str, str | None]:
    """Queue for a concurrency slot, then call upstream with retries.

    Transient failures (network drop mid-SSE, 401 with token re-mint, 429/5xx,
    empty result) are retried up to RETRIES times with backoff. `alive` is a
    zero-arg callable polled while queued and between attempts so we stop
    burning quota for clients that already hung up.
    """
    _stat("queued", 1)
    try:
        while not _gen_sem.acquire(timeout=2.0):  # poll so we can notice dead clients
            if alive and not alive():
                raise ClientDisconnected("client hung up while queued")
    finally:
        _stat("queued", -1)
    _stat("active", 1)
    try:
        last_err: Exception = RuntimeError("unreachable")
        for attempt in range(RETRIES + 1):
            if alive and not alive():
                raise ClientDisconnected("client hung up before generation finished")
            try:
                return _attempt_generate(prompt, ref_parts, size, quality,
                                         moderation, tool_extra, effort)
            except UpstreamError as e:
                if e.code == 401:
                    try:
                        get_auth(force_refresh=True)  # token bad despite future exp
                    except Exception as re_err:
                        # can't re-mint the token → every retry would resend the
                        # same dead token; fail fast instead of burning RETRIES.
                        raise UpstreamError(401, f"token refresh failed: {re_err}") from None
                elif not (e.code in (408, 409, 425, 429) or e.code >= 500):
                    raise  # 4xx other than auth/rate-limit: retrying won't help
                last_err = e
            except (URLError, OSError, http.client.HTTPException, RuntimeError) as e:
                last_err = e  # transport drop / SSE cut / empty result -> retry
            if attempt < RETRIES:
                wait = getattr(last_err, "retry_after", None) or RETRY_BACKOFF * (2 ** attempt)
                wait = min(wait, 60.0)  # clamp untrusted upstream Retry-After (no slot-pinning sleeps)
                print(f"[retry] attempt {attempt + 1}/{RETRIES} failed "
                      f"({str(last_err)[:160]}); retrying in {wait:.0f}s", flush=True)
                time.sleep(wait)
        raise last_err
    finally:
        _stat("active", -1)
        _gen_sem.release()


def generate_images(prompt: str, ref_parts: list[dict], size: str, quality: str,
                    moderation: str, tool_extra: dict, n: int,
                    alive=None, effort: str = "low",
                    ) -> tuple[list[tuple[str, str | None]], list[Exception]]:
    """Generate n images. Returns (successes, errors) — a batch where at least
    one image succeeded is served rather than discarded wholesale."""
    if n <= 1:
        return [_generate_one(prompt, ref_parts, size, quality,
                              moderation, tool_extra, alive, effort)], []
    workers = min(n, MAX_CONCURRENCY)
    results: list[tuple[str, str | None]] = []
    errors: list[Exception] = []
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [ex.submit(_generate_one, prompt, ref_parts, size, quality,
                          moderation, tool_extra, alive, effort) for _ in range(n)]
        for f in futs:
            try:
                results.append(f.result())
            except ClientDisconnected:
                raise
            except Exception as e:
                errors.append(e)
    finally:
        # On bail (e.g. client disconnect) don't block on in-flight workers;
        # cancel queued-but-unstarted futures so they stop burning quota.
        ex.shutdown(wait=False, cancel_futures=True)
    if not results:
        # surface the most informative error (an upstream HTTP error beats a
        # transient transport blip), not just the first-submitted one.
        raise next((e for e in errors if isinstance(e, UpstreamError)), errors[0])
    return results, errors


# --------------------------------------------------------------------------- #
# Output formatting (b64_json | url)
# --------------------------------------------------------------------------- #
def _save_image(b64: str, ext: str = ".png") -> str:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    img = base64.b64decode(b64)
    name = f"{int(time.time())}-{hashlib.sha1(img).hexdigest()[:12]}{ext}"
    (IMAGE_DIR / name).write_bytes(img)
    return f"{PUBLIC_BASE}/images/{name}"


def _to_data_items(results: list[tuple[str, str | None]], response_format: str,
                   output_format: str | None) -> list[dict]:
    ext = FORMAT_EXT.get(output_format or "png", ".png")
    items = []
    for b64, revised in results:
        item = {"url": _save_image(b64, ext)} if response_format == "url" else {"b64_json": b64}
        if revised:
            item["revised_prompt"] = revised
        items.append(item)
    return items


# --------------------------------------------------------------------------- #
# multipart/form-data parsing (binary-safe, zero-dependency)
# --------------------------------------------------------------------------- #
def _disp_param(disp: str, key: str) -> str | None:
    # anchor on a param boundary (start or ";") so searching for `name` doesn't
    # match the `name="..."` substring inside `filename="..."`.
    m = re.search(rf'(?:^|;)\s*{re.escape(key)}="([^"]*)"', disp)
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


def _coerce_compression(raw) -> int | None:
    """Parse output_compression; None when absent, -1 sentinel on bad input."""
    if raw is None or raw == "":
        return None
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
        "n": _coerce_n(m.get("n", 1)),  # absent -> 1; n=0 kept so it fails the 1..MAX_N check
        "response_format": m.get("response_format") or "b64_json",
        # default fast passthrough: callers that don't pass effort skip think+expand
        # (pass effort="medium"/"high" explicitly for the deep think+expand path)
        "effort": m.get("effort") or "low",
        # optional passthroughs — only sent upstream when explicitly provided
        "background": m.get("background") or None,
        "output_format": m.get("output_format") or None,
        "output_compression": _coerce_compression(m.get("output_compression")),
        "input_fidelity": m.get("input_fidelity") or None,
    }


# --------------------------------------------------------------------------- #
# HTTP server (OpenAI-compatible surface)
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = f"codex-image-api/{__version__}"
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
        """Read the body; send 400/413 + return None on bad/oversized length."""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = -1
        if length < 0:  # non-numeric or negative — don't let rfile.read(-1) drain to EOF
            self.close_connection = True
            self._send_error(400, "invalid Content-Length header")
            return None
        if length > MAX_BODY_BYTES:
            self.close_connection = True  # don't desync the keep-alive stream
            self._send_error(413, f"request body exceeds {MAX_BODY_BYTES} bytes",
                             "payload_too_large")
            return None
        return self.rfile.read(length) if length else b""

    def _host_ok(self) -> bool:
        """Reject Host headers we don't recognize (anti DNS-rebinding)."""
        host = self.headers.get("Host", "")
        return _host_only(host) in ALLOWED_HOSTS or host in ALLOWED_HOSTS

    def log_message(self, format, *args):  # noqa: A002 (match base signature)
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _common(self, prompt, ref_parts, size, quality, moderation, n, response_format,
                background=None, output_format=None, output_compression=None,
                input_fidelity=None, effort="low"):
        """Shared generation + response path for generations and edits."""
        if not isinstance(prompt, str) or not prompt.strip():
            return self._send_error(400, "`prompt` is required and must be a non-empty string")
        if size not in VALID_SIZES:
            return self._send_error(400, f"invalid size {size!r}; allowed: {sorted(VALID_SIZES)}")
        if quality not in VALID_QUALITY:
            return self._send_error(400, f"invalid quality {quality!r}")
        if moderation not in VALID_MODERATION:
            return self._send_error(400, f"invalid moderation {moderation!r}; allowed: low/auto")
        if effort not in VALID_EFFORT:
            return self._send_error(400, f"invalid effort {effort!r}; allowed: low/medium/high")
        if background is not None and background not in VALID_BACKGROUND:
            return self._send_error(
                400, f"invalid background {background!r}; allowed: transparent/opaque/auto")
        if output_format is not None and output_format not in VALID_OUTPUT_FORMAT:
            return self._send_error(
                400, f"invalid output_format {output_format!r}; allowed: png/jpeg/webp")
        if output_compression is not None and not (0 <= output_compression <= 100):
            return self._send_error(400, "`output_compression` must be an integer 0-100")
        if input_fidelity is not None and input_fidelity not in VALID_FIDELITY:
            return self._send_error(
                400, f"invalid input_fidelity {input_fidelity!r}; allowed: low/high")
        if not (1 <= n <= MAX_N):
            return self._send_error(400, f"`n` must be between 1 and {MAX_N}")
        if response_format not in ("b64_json", "url"):
            return self._send_error(400, "`response_format` must be 'b64_json' or 'url'")

        tool_extra = {k: v for k, v in {
            "background": background, "output_format": output_format,
            "output_compression": output_compression, "input_fidelity": input_fidelity,
        }.items() if v is not None}

        t0 = time.time()
        tag = (f"quality={quality} size={size} n={n} effort={effort} refs={len(ref_parts)}"
               + (f" {tool_extra}" if tool_extra else ""))
        alive = lambda conn=self.connection: not _client_gone(conn)  # noqa: E731
        try:
            results, errors = generate_images(prompt, ref_parts, size, quality,
                                              moderation, tool_extra, n, alive, effort)
        except ClientDisconnected as e:
            self.close_connection = True
            print(f"[gen] aborted after {time.time() - t0:.1f}s ({tag}): {e} — "
                  "raise the client-side timeout if this was not intentional", flush=True)
            return
        except UpstreamError as e:
            print(f"[gen] failed after {time.time() - t0:.1f}s ({tag}): {e}", flush=True)
            return self._send_error(502, str(e), "upstream_error")
        except URLError as e:
            print(f"[gen] failed after {time.time() - t0:.1f}s ({tag}): {e}", flush=True)
            return self._send_error(502, f"network error: {e.reason}", "upstream_error")
        except Exception as e:
            print(f"[gen] failed after {time.time() - t0:.1f}s ({tag}): {e}", flush=True)
            return self._send_error(502, f"generation failed: {e}", "upstream_error")

        out = {
            "created": int(time.time()),
            "data": _to_data_items(results, response_format, output_format),
            "usage": {},
        }
        if errors:  # partial batch: serve what succeeded, surface the rest
            out["warnings"] = [f"{len(errors)}/{n} generations failed: "
                               + "; ".join(str(e)[:160] for e in errors[:3])]
        elapsed = time.time() - t0
        print(f"[gen] ok {len(results)}/{n} in {elapsed:.1f}s ({tag})", flush=True)
        try:
            self._send_json(200, out)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            print(f"[gen] client disconnected before the response could be sent "
                  f"(generation took {elapsed:.1f}s — raise the client-side timeout)",
                  flush=True)

    # -- routes ----------------------------------------------------------- #
    def do_GET(self):
        if not self._host_ok():
            return self._send_error(403, "host not allowed", "forbidden")
        if self.path == "/health":
            try:
                # read-only: a health probe must never trigger a network token
                # refresh (could block ~30s) nor leak auth.json search paths.
                if _cached:
                    exp = _cached.get("exp")
                else:
                    _, data = _read_auth_file()
                    exp = _codex_auth.jwt_exp((data.get("tokens") or {}).get("access_token"))
                if exp:
                    rem = int(exp - time.time())
                    detail = (f"expires in {rem}s" if rem > 0
                              else f"expired {-rem}s ago; refreshes on next request")
                else:
                    detail = "loaded"
                with _stats_lock:
                    active, queued = _stats["active"], _stats["queued"]
                self._send_json(200, {"ok": True, "auth": detail,
                                      "model": ORCHESTRATION_MODEL,
                                      "concurrency": MAX_CONCURRENCY,
                                      "active": active, "queued": queued,
                                      "uptime_s": int(time.time() - _started_at),
                                      "version": __version__})
            except Exception:
                self._send_json(200, {"ok": False, "auth": "unavailable; run `codex login`"})
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
        self.send_header("Content-Type", EXT_MIME.get(fpath.suffix.lower(), "image/png"))
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
        if route == "/v1/responses":
            return self._handle_responses()
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
                        if len(ref_parts) >= MAX_REF_IMAGES:  # cap before decoding more
                            return self._send_error(400, f"too many image parts (max {MAX_REF_IMAGES})")
                        ref_parts.append(_image_part_from_bytes(p["data"], p["content_type"]))
                elif p["name"]:
                    fields[p["name"]] = p["data"].decode("utf-8", "replace")
        except Exception as e:
            return self._send_error(400, f"bad image upload: {e}")
        if not ref_parts:
            return self._send_error(400, "at least one `image` file part is required")
        self._common(ref_parts=ref_parts, **_extract_params(fields))

    def _handle_responses(self):
        """OpenAI Responses-API compat: `client.responses.create` + an
        `image_generation` tool. Lets a downstream that targets the official
        Responses API (route B) hit codex with ZERO code change — it only swaps
        OPENAI_BASE_URL. Translates the request into the same generate_images()
        engine /v1/images uses, then returns a single Response-shaped JSON the
        OpenAI SDK can deserialize. Does not touch _common / /v1/images."""
        body = self._read_body()
        if body is None:
            return  # 400/413 already sent
        try:
            req = json.loads(body or b"{}")
        except Exception:
            return self._send_error(400, "invalid JSON body")

        # 1. prompt + reference images from `input` (robust to content shapes:
        #    content as list-of-parts OR bare str; image_url as str OR {"url":...}).
        prompt_parts: list[str] = []
        ref_specs: list[str] = []
        for msg in (req.get("input") or []):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                prompt_parts.append(content)
                continue
            for part in (content or []):
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in ("input_text", "text") and isinstance(part.get("text"), str):
                    prompt_parts.append(part["text"])
                elif ptype in ("input_image", "image"):
                    url = part.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url")
                    if isinstance(url, str) and url:
                        ref_specs.append(url)
        prompt = "\n".join(p for p in prompt_parts if p).strip()

        # 2. image_generation tool params + reasoning.effort. Top-level `model`
        #    (e.g. the gpt-5.5 director) is ignored — codex uses ORCHESTRATION_MODEL.
        tool = next((t for t in (req.get("tools") or [])
                     if isinstance(t, dict) and t.get("type") == "image_generation"), {})
        size = tool.get("size") or "1024x1024"
        quality = tool.get("quality") or "high"
        moderation = tool.get("moderation") or "low"
        background = tool.get("background") or None
        output_format = tool.get("output_format") or None
        output_compression = _coerce_compression(tool.get("output_compression"))
        input_fidelity = tool.get("input_fidelity") or None
        reasoning = req.get("reasoning")
        effort = (reasoning.get("effort") if isinstance(reasoning, dict) else None) or "low"

        # 3. resolve refs + validate (mirror _common; n is fixed = 1 for route B).
        if len(ref_specs) > MAX_REF_IMAGES:
            return self._send_error(400, f"too many input_image parts (max {MAX_REF_IMAGES})")
        try:
            ref_parts = [_resolve_reference(s) for s in ref_specs]
        except Exception as e:
            return self._send_error(400, f"bad input_image: {e}")
        if not prompt:
            return self._send_error(400, "no input_text found in `input`")
        if size not in VALID_SIZES:
            return self._send_error(400, f"invalid size {size!r}; allowed: {sorted(VALID_SIZES)}")
        if quality not in VALID_QUALITY:
            return self._send_error(400, f"invalid quality {quality!r}")
        if moderation not in VALID_MODERATION:
            return self._send_error(400, f"invalid moderation {moderation!r}; allowed: low/auto")
        if effort not in VALID_EFFORT:
            return self._send_error(400, f"invalid effort {effort!r}; allowed: low/medium/high")
        if background is not None and background not in VALID_BACKGROUND:
            return self._send_error(
                400, f"invalid background {background!r}; allowed: transparent/opaque/auto")
        if output_format is not None and output_format not in VALID_OUTPUT_FORMAT:
            return self._send_error(
                400, f"invalid output_format {output_format!r}; allowed: png/jpeg/webp")
        if output_compression is not None and not (0 <= output_compression <= 100):
            return self._send_error(400, "`output_compression` must be an integer 0-100")
        if input_fidelity is not None and input_fidelity not in VALID_FIDELITY:
            return self._send_error(
                400, f"invalid input_fidelity {input_fidelity!r}; allowed: low/high")

        tool_extra = {k: v for k, v in {
            "background": background, "output_format": output_format,
            "output_compression": output_compression, "input_fidelity": input_fidelity,
        }.items() if v is not None}

        # 4. generate (n=1) — same engine as /v1/images, with disconnect detection.
        t0 = time.time()
        tag = (f"[responses] quality={quality} size={size} effort={effort} "
               f"refs={len(ref_parts)}" + (f" {tool_extra}" if tool_extra else ""))
        alive = lambda conn=self.connection: not _client_gone(conn)  # noqa: E731
        try:
            results, _ = generate_images(prompt, ref_parts, size, quality,
                                         moderation, tool_extra, 1, alive, effort)
        except ClientDisconnected as e:
            self.close_connection = True
            print(f"[gen] aborted after {time.time() - t0:.1f}s ({tag}): {e}", flush=True)
            return
        except UpstreamError as e:
            print(f"[gen] failed after {time.time() - t0:.1f}s ({tag}): {e}", flush=True)
            return self._send_error(502, str(e), "upstream_error")
        except URLError as e:
            print(f"[gen] failed after {time.time() - t0:.1f}s ({tag}): {e}", flush=True)
            return self._send_error(502, f"network error: {e.reason}", "upstream_error")
        except Exception as e:
            print(f"[gen] failed after {time.time() - t0:.1f}s ({tag}): {e}", flush=True)
            return self._send_error(502, f"generation failed: {e}", "upstream_error")

        if not results:
            return self._send_error(502, "no image produced", "upstream_error")
        b64, revised = results[0]

        # 5. assemble a Response-shaped JSON the OpenAI SDK can deserialize.
        #    result = PURE base64 (no data: prefix) — downstream b64decode's it.
        digest = hashlib.sha1(b64.encode()).hexdigest()[:24]
        img_item: dict = {
            "type": "image_generation_call",
            "id": f"ig_{digest}",
            "status": "completed",
            "result": b64,
        }
        if revised:  # extra field; SDK is extra="allow", downstream reads via getattr
            img_item["revised_prompt"] = revised
        out = {
            "id": f"resp_{digest}",
            "object": "response",
            "created_at": time.time(),
            "model": ORCHESTRATION_MODEL,
            "status": "completed",
            "output": [img_item],
            "parallel_tool_calls": True,   # these three are required, non-Optional in the SDK
            "tool_choice": "auto",
            "tools": req.get("tools") or [],
            "usage": None,
        }
        elapsed = time.time() - t0
        print(f"[gen] ok 1/1 in {elapsed:.1f}s ({tag})", flush=True)
        try:
            self._send_json(200, out)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            print(f"[gen] client disconnected before the response could be sent "
                  f"({elapsed:.1f}s)", flush=True)


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Clients that time out and hang up mid-generation are an expected,
        # recoverable event — one log line, not a 30-line traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            print(f"[http] client {client_address[0]}:{client_address[1]} "
                  f"disconnected mid-request ({type(exc).__name__})", flush=True)
            return
        super().handle_error(request, client_address)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # logs reach launchd files promptly
    except Exception:
        pass
    try:
        httpd = _Server((HOST, PORT), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            # launchd KeepAlive / skill auto-start / run.sh can race each other;
            # losing the port to a healthy sibling is fine — bow out quietly.
            print(f"port {PORT} already in use — another codex-image-api instance "
                  "is probably serving; exiting.", flush=True)
            raise SystemExit(0)
        raise
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        print(f"⚠️  bound to non-localhost {HOST!r}: the Host-header allowlist is NOT "
              "authentication — do not expose this port to an untrusted network "
              "(anyone who reaches it spends your ChatGPT subscription quota).", flush=True)
    print(f"codex-image-api {__version__} listening on http://{HOST}:{PORT}  "
          f"(concurrency={MAX_CONCURRENCY}, retries={RETRIES})")
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
