#!/usr/bin/env bash
# codex-image-api control script.
#   ./run.sh                  run in foreground
#   ./run.sh bg               run detached in background
#   ./run.sh install-launchd  install + load the macOS LaunchAgent (boot autostart)
set -euo pipefail
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
cd "$(dirname "$0")"

case "${1:-}" in
  bg)
    nohup python3 server.py > /tmp/codex-image-api.log 2>&1 &
    echo "started in background (pid $!), logs: /tmp/codex-image-api.log"
    echo "stop with: pkill -f 'python3 server.py'"
    ;;
  install-launchd)
    python3 -c "import lib_preflight as p; p._install_launchd_plist()"
    ;;
  "")
    exec python3 server.py
    ;;
  *)
    echo "usage: ./run.sh [bg|install-launchd]" >&2
    exit 2
    ;;
esac
