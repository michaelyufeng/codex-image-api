#!/usr/bin/env python3
"""connect-api skill — point an existing project's OpenAI image calls at the
local codex-image-api (subscription-backed gpt-image-2), usually with zero code
changes, by writing OPENAI_BASE_URL into the project's .env.

Usage:  python3 connect.py <project_dir>
Zero third-party dependencies.
"""
import argparse
import os
import re
import sys
from pathlib import Path

# Shared preflight (login check + server auto-start) lives in the plugin root.
# This script sits at <root>/skills/connect-api/connect.py, so the backend
# (lib_preflight.py, server.py) is two levels up. Resolving relative to __file__
# keeps it working wherever the plugin is installed; CODEX_IMAGE_SERVER_DIR
# overrides for a dev checkout elsewhere.
_BACKEND = os.environ.get("CODEX_IMAGE_SERVER_DIR") or str(Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, _BACKEND)
import lib_preflight as preflight  # type: ignore  # noqa: E402

KEY = "OPENAI_BASE_URL"


def write_env_key(env_file: Path, key: str, val: str) -> str:
    """Idempotently set `key=val` in a .env file, preserving comments/other lines.

    Returns one of: "created" | "updated" | "skipped".
    """
    line = f"{key}={val}"
    if not env_file.exists():
        env_file.write_text(line + "\n", encoding="utf-8")
        return "created"
    text = env_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    for i, ln in enumerate(lines):
        if pat.match(ln):
            if ln.strip() == line:
                return "skipped"
            lines[i] = line
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return "updated"
    sep = "" if (text == "" or text.endswith("\n")) else "\n"
    env_file.write_text(text + sep + line + "\n", encoding="utf-8")
    return "updated"


def main() -> None:
    ap = argparse.ArgumentParser(description="Connect a project to the local codex-image-api")
    ap.add_argument("project", help="path to the project directory to connect")
    a = ap.parse_args()

    project = Path(a.project).expanduser().resolve()
    if not project.is_dir():
        sys.exit(f"项目路径不存在或不是目录: {project}")

    # 1) 登录验证（配置工具：仅提示，不自动弹浏览器）
    preflight.require_auth(on_not_logged_in="prompt_only")

    # 2) 确保 server 在线（按需拉起，不主动装 launchd）
    preflight.ensure_server_running(install_launchd=False)

    # 3) shell 环境变量覆盖告警（.env 优先级低于已存在的 shell env）
    shell_val = os.environ.get(KEY)
    if shell_val and shell_val != preflight.API_BASE_URL:
        print(f"⚠️  当前 shell 已设 {KEY}={shell_val!r}，会盖过 .env；"
              "如需用本地 API，请同步更新你的 shell 配置（~/.zshrc 等）")

    # 4) 幂等写 .env
    env_file = project / ".env"
    action = write_env_key(env_file, KEY, preflight.API_BASE_URL)
    label = {"created": "已创建并写入", "updated": "已写入", "skipped": "已是目标值，跳过"}[action]
    print(f"[{label}] {env_file}: {KEY}={preflight.API_BASE_URL}")

    # 5) 连通确认
    r = preflight.probe_api()
    if r and r.get("ok"):
        print(f"[OK] 本地 API 在线: model={r.get('model')} auth={r.get('auth')}")
    else:
        sys.exit("[错误] 本地 API 探测失败，请查看 /tmp/codex-image-api.log")

    # 6) 通用成本提示（适用于任何带成本看板/预算 guard 的项目）
    print("\n[提示] 切到本地后实际不再产生 OpenAI Images API 费用。若你的项目内置了")
    print("  按官方价记账的成本看板或预算 guard，数字会虚高、甚至可能按预测成本误拦——")
    print("  需要时把相应阈值调大，或把成本逻辑改成订阅模式记 0。")

    # 7) 完成
    print(f"\n[完成]「{project.name}」已接入 codex-image-api（base_url={preflight.API_BASE_URL}）")
    print("  下次运行该项目即走本地 API（OPENAI_API_KEY 保留原值，本地会忽略）")
    print(f'  如需开机自启常驻，运行: (cd "{preflight.SERVER_DIR}" && ./run.sh install-launchd)')


if __name__ == "__main__":
    main()
