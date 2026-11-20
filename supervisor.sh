#!/bin/bash
# agent-process-supervisor — shell wrapper around supervisor.py
#
# Usage: ./supervisor.sh [check|cron|status|daemon|restart|stop] [--config PATH]
#
#   check / cron   one check-and-restart pass (intended for crontab)
#   status         report status only, restart nothing
#   daemon         loop forever (interval from config)
#   restart        stop all tracked processes, then one check-and-restart pass
#   stop           stop all tracked command processes (systemd units untouched)
#
# Config: --config PATH, $APS_CONFIG, or ./config.json next to this script.
# If ./supervisor.env exists next to this script it is sourced first so
# the supervised processes inherit its variables (cron does not load
# your shell environment automatically).
#
# Crontab (every 5 minutes):
#   */5 * * * * /path/to/agent-process-supervisor/supervisor.sh cron
#
# All paths are relative to this script's directory unless overridden
# in the config or by environment variables. Nothing here is hardcoded
# to a specific machine or user.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── Optional env file ─────────────────────────────────────────────
if [[ -f "$SCRIPT_DIR/supervisor.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/supervisor.env"
    set +a
fi

# ─── Config ────────────────────────────────────────────────────────
CONFIG="${APS_CONFIG:-$SCRIPT_DIR/config.json}"
EXTRA_ARGS=()
CMD="${1:-check}"
if [[ "$CMD" == --config ]]; then
    CONFIG="$2"
    CMD="${3:-check}"
    shift 3 || true
elif [[ "${2:-}" == "--config" ]]; then
    CONFIG="$3"
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "config not found: $CONFIG" >&2
    echo "Copy config.example.json to config.json and edit it." >&2
    exit 2
fi

PY="${APS_PYTHON:-python3}"

case "$CMD" in
    check|cron)
        exec "$PY" "$SCRIPT_DIR/supervisor.py" --config "$CONFIG" --once
        ;;
    status)
        exec "$PY" "$SCRIPT_DIR/supervisor.py" --config "$CONFIG" --check
        ;;
    daemon)
        exec "$PY" "$SCRIPT_DIR/supervisor.py" --config "$CONFIG" --daemon
        ;;
    restart)
        "$PY" "$SCRIPT_DIR/supervisor.py" --config "$CONFIG" --stop || true
        sleep 1
        exec "$PY" "$SCRIPT_DIR/supervisor.py" --config "$CONFIG" --once
        ;;
    stop)
        exec "$PY" "$SCRIPT_DIR/supervisor.py" --config "$CONFIG" --stop
        ;;
    *)
        echo "agent-process-supervisor"
        echo ""
        echo "Usage: $0 {check|cron|status|daemon|restart|stop} [--config PATH]"
        echo ""
        echo "  check / cron  one check-and-restart pass (default)"
        echo "  status        report status only, restart nothing"
        echo "  daemon        loop forever (interval from config)"
        echo "  restart       stop all, then one check-and-restart pass"
        echo "  stop          stop all tracked command processes"
        echo ""
        echo "Config: $CONFIG"
        echo "State:  \${APS_STATE_DIR:-\$HOME/.local/state/agent-process-supervisor}"
        echo ""
        echo "Crontab setup (every 5 min):"
        echo "  */5 * * * * $SCRIPT_DIR/supervisor.sh cron"
        exit 1
        ;;
esac
