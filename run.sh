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
    if [[ -f "$PIDFILE" ]] && kill "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "stopped (pid $(cat "$PIDFILE"))."
    else
      # The process shows up as `.../Python server.py`, NOT `python3 server.py`,
      # so a pattern fallback must match on server.py. launchd-managed instances:
      # stop with `launchctl unload ~/Library/LaunchAgents/com.codex-image-api.plist`.
      echo "no tracked background server (pidfile: $PIDFILE)." >&2
      echo "if started another way, stop with: pkill -f 'server\\.py'" >&2
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
