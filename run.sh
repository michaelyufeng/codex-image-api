#!/usr/bin/env bash
# codex-image-api control script.
#   ./run.sh                  run in foreground
#   ./run.sh bg               run detached in background
#   ./run.sh stop             stop the background server started by `bg`
#   ./run.sh install-launchd  install + load the macOS LaunchAgent (boot autostart)
set -euo pipefail
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
cd "$(dirname "$0")"

PIDFILE=/tmp/codex-image-api.pid

case "${1:-}" in
  bg)
    nohup python3 server.py > /tmp/codex-image-api.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "started in background (pid $!), logs: /tmp/codex-image-api.log"
    echo "stop with: ./run.sh stop"
    ;;
  stop)
    # Prefer the pidfile (written by `bg`). The skill auto-start uses Popen with
    # no pidfile and the process shows as `.../python <abs>/server.py` (NOT
    # `python3 server.py`), so fall back to matching server.py. launchd-managed
    # instances: `launchctl unload ~/Library/LaunchAgents/com.codex-image-api.plist`.
    if [[ -f "$PIDFILE" ]] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "stopped (pid $(cat "$PIDFILE"))."
    elif pkill -f "$PWD/server.py"; then
      echo "stopped (matched $PWD/server.py)."
    else
      echo "no running codex-image-api server found." >&2
    fi
    rm -f "$PIDFILE"
    ;;
  install-launchd)
    python3 -c "import lib_preflight as p; p._install_launchd_plist()"
    ;;
  "")
    exec python3 server.py
    ;;
  *)
    echo "usage: ./run.sh [bg|stop|install-launchd]" >&2
    exit 2
    ;;
esac
