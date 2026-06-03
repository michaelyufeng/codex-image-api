#!/usr/bin/env python3
"""Shared preflight for the codex-image-api skills.

Both `generate-image` and `connect-api` skills import this module to:
  - check Codex login state (not-installed / not-logged-in / logged-in),
  - guide the user when auth is missing,
  - ensure the local API server is running (single source of auto-start logic).

Zero third-party dependencies (standard library only).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import _codex_auth  # type: ignore  # local: shared auth.json + JWT helpers

# --------------------------------------------------------------------------- #
# Constants — single source of truth shared by both skills
# --------------------------------------------------------------------------- #
SERVER_HOST = os.environ.get("CODEX_IMAGE_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("CODEX_IMAGE_PORT", "10532"))
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
API_BASE_URL = f"{SERVER_URL}/v1"  # the value connect.py writes into a project's .env
SERVER_DIR = os.environ.get("CODEX_IMAGE_SERVER_DIR") or os.path.dirname(os.path.abspath(__file__))

class AuthState(Enum):
    CODEX_NOT_INSTALLED = "codex_not_installed"
    NOT_LOGGED_IN = "not_logged_in"
    LOGGED_IN = "logged_in"


@dataclass
class PreflightResult:
    auth: AuthState
    token_ttl_seconds: int | None = None  # remaining lifetime, only when LOGGED_IN


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Auth detection
# --------------------------------------------------------------------------- #
def _read_access_token() -> str | None:
    for p in _codex_auth.AUTH_CANDIDATES:
        if not p:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            tok = (data.get("tokens") or {}).get("access_token")
            if tok:
                return tok
        except Exception:
            continue
    return None


def check_auth() -> PreflightResult:
    """Local, network-free three-state detection of Codex auth."""
    if shutil.which("codex") is None:
        return PreflightResult(AuthState.CODEX_NOT_INSTALLED)
    token = _read_access_token()
    if not token:
        return PreflightResult(AuthState.NOT_LOGGED_IN)
    exp = _codex_auth.jwt_exp(token)
    # If we cannot read exp, trust the file's presence (server.py will refresh).
    if exp is None:
        return PreflightResult(AuthState.LOGGED_IN, None)
    if exp <= time.time():
        # Expired access token; server.py can still refresh via refresh_token,
        # but surface it as needs-login so the user can re-auth if refresh fails.
        return PreflightResult(AuthState.NOT_LOGGED_IN)
    return PreflightResult(AuthState.LOGGED_IN, int(exp - time.time()))


def require_auth(on_not_logged_in: str = "prompt_only") -> None:
    """Gate a skill on Codex auth. Returns on success, sys.exit on failure.

    on_not_logged_in: "auto_login" (run `codex login`, then re-check) or
                      "prompt_only" (print instructions and exit).
    Not-installed is always prompt-only (we never auto-install Codex).
    """
    r = check_auth()
    if r.auth is AuthState.LOGGED_IN:
        return

    if r.auth is AuthState.CODEX_NOT_INSTALLED:
        _log("✗ 未检测到 Codex CLI。请先安装并登录：")
        _log("    npm install -g @openai/codex")
        _log("    codex login")
        sys.exit(1)

    # NOT_LOGGED_IN
    if on_not_logged_in == "auto_login":
        _log("✗ Codex 未登录，正在打开 `codex login`（浏览器授权后会自动继续）…")
        try:
            subprocess.run(["codex", "login"], check=False)
        except Exception as e:
            _log(f"  启动 codex login 失败: {e}")
        if check_auth().auth is AuthState.LOGGED_IN:
            _log("✓ 登录成功。")
            return
        _log("✗ 仍未登录，请手动完成 `codex login` 后重试。")
        sys.exit(1)

    _log("✗ Codex 未登录。请运行 `codex login` 完成授权后重试。")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Server lifecycle
# --------------------------------------------------------------------------- #
def probe_api(timeout: float = 5.0) -> dict | None:
    """GET /health → parsed JSON dict, or None if unreachable."""
    try:
        with urllib.request.urlopen(f"{SERVER_URL}/health", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _server_online(timeout: float = 2.0) -> bool:
    r = probe_api(timeout=timeout)
    return bool(r and r.get("ok"))


def ensure_server_running(install_launchd: bool = False, wait_seconds: int = 15) -> None:
    """Ensure the API is reachable; start it if not. Single source of auto-start."""
    if _server_online():
        return

    if install_launchd:
        _install_launchd_plist()
    else:
        server = os.path.join(SERVER_DIR, "server.py")
        if not os.path.isfile(server):
            sys.exit(f"server.py not found at {server!r}; set CODEX_IMAGE_SERVER_DIR")
        _log("codex-image-api 未在线，正在自动启动…")
        log = open("/tmp/codex-image-api.log", "a")
        subprocess.Popen([sys.executable, server], cwd=SERVER_DIR,
                         stdout=log, stderr=log, start_new_session=True)

    for _ in range(wait_seconds * 2):  # poll every 0.5s
        if _server_online():
            _log("已就绪。")
            return
        time.sleep(0.5)
    sys.exit(f"API 未能在 {wait_seconds} 秒内启动，请查看 /tmp/codex-image-api.log")


def _install_launchd_plist() -> None:
    """Idempotently install + load the LaunchAgent (macOS)."""
    label = "com.codex-image-api"
    src = Path(SERVER_DIR) / "deploy" / f"{label}.plist"
    dst = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if not src.is_file():
        _log(f"✗ 找不到 plist 模板: {src}")
        return
    try:
        # Fill the template's placeholders with this machine's real paths.
        content = (src.read_text(encoding="utf-8")
                   .replace("__PYTHON__", sys.executable)
                   .replace("__SERVER_DIR__", SERVER_DIR))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        _log(f"已安装 LaunchAgent → {dst}")
        # load (ignore 'already loaded'), then start
        subprocess.run(["launchctl", "load", "-w", str(dst)],
                       capture_output=True, text=True)
        subprocess.run(["launchctl", "start", label], capture_output=True, text=True)
        _log(f"已 load + start {label}")
    except Exception as e:
        _log(f"launchd 安装出错（可手动 launchctl load）: {e}")


if __name__ == "__main__":
    # Quick self-check: `python3 lib_preflight.py`
    res = check_auth()
    print(f"auth={res.auth.value} ttl={res.token_ttl_seconds} server_online={_server_online()}")
