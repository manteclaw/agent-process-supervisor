---
name: process-supervisor
description: Config-driven process supervisor for agents. Watches the processes listed in a JSON config and restarts the ones that died, with exponential backoff. Use when an agent runs long-lived background processes (servers, workers, daemons, systemd units) that must stay up, or when a process keeps dying and needs a supervised restart instead of a manual one.
license: MIT
compatibility: Any agent runtime with a shell and Python 3.9+ (Linux/macOS; systemd type needs systemd)
metadata:
  author: manteclaw
  version: "1.0.0"
  tags: supervisor watchdog processes daemon restart systemd monitoring
---

## Process Supervisor

**When a background process an agent depends on is dead or keeps dying, supervise it — do not restart it by hand and hope.** This skill watches every process in a JSON config and restarts the dead ones, with exponential backoff so a crashing process does not spin forever.

### Setup

1. Copy `config.example.json` to `config.json` and list the processes to watch. Every process needs a `name`, a `type` (`command` or `systemd`), and a `restart_policy` (`always` or `never` for monitor-only).
2. Run `./supervisor.sh check` once to verify the config. It prints one line per process: `RUNNING`, `DEAD`, `RESTARTED`, or `RESTART_FAILED`.
3. Put it on a schedule — cron every 5 minutes, or `./supervisor.sh daemon` for a continuous loop.

### When to use

- A server, worker, or daemon the agent runs must stay up across sessions.
- A process died and you are about to restart it manually for the second time — supervise it instead.
- You need a read-only health report: `./supervisor.sh status` (or `supervisor.py --check`) restarts nothing and exits 1 if anything is dead, so it works as a monitoring check.

### How it works

- **Command processes** are started from the config (`command`, `cwd`, `env`, `log_file`) and tracked by PID in the state directory. A process that exits within 2 seconds of starting counts as a failed start, not a running process.
- **Systemd processes** are checked with `systemctl is-active` and restarted with `systemctl restart`.
- **Health checks** are optional per process: a `health_check` command (exit 0 = healthy) and/or a `port` (TCP connect to 127.0.0.1). A running process that fails its health check is treated as dead and restarted.
- **Backoff:** after a failed restart, the next attempt waits `base_seconds * multiplier ^ consecutive_failures`, capped at `max_seconds` (defaults 60s / 2x / 480s). The supervisor reports `DEAD (BACKOFF)` while waiting.
- **State:** PIDs, failure counts, and a JSONL event log live under `state_dir` (default `~/.local/state/agent-process-supervisor`). Nothing is written anywhere else.
- **All paths are config or env vars.** Relative paths in the config resolve against the config file's directory. There are no hardcoded machine paths.

### Config shape

```json
{
  "state_dir": "~/.local/state/agent-process-supervisor",
  "interval_seconds": 30,
  "backoff": {"base_seconds": 60, "max_seconds": 480, "multiplier": 2},
  "processes": [
    {
      "name": "api-server",
      "type": "command",
      "command": ["python3", "server.py"],
      "cwd": "/srv/my-app",
      "env": {"PORT": "8000"},
      "log_file": "logs/api-server.log",
      "restart_policy": "always",
      "port": 8000
    },
    {
      "name": "nginx",
      "type": "systemd",
      "unit": "nginx",
      "restart_policy": "always"
    }
  ]
}
```

### Commands

| Command | What it does |
|---|---|
| `./supervisor.sh check` (or `cron`) | One check-and-restart pass |
| `./supervisor.sh status` | Report only, restart nothing, exit 1 if anything dead |
| `./supervisor.sh daemon` | Loop forever (interval from config) |
| `./supervisor.sh restart` | Stop all tracked processes, then one pass |
| `./supervisor.sh stop` | Stop all tracked command processes |
| `python3 supervisor.py --check` | Same as `status`; `--dry-run` is an alias |
| `python3 supervisor.py --once` | Same as `check` |
| `python3 supervisor.py --daemon [--interval N]` | Same as `daemon` |

### Rules

- **Never add a process you do not own to the config.** Supervising someone else's service and restarting it is not your call.
- **Monitor-only first:** when adding a process you are unsure about, set `restart_policy` to `"never"` and watch a few cycles before enabling restarts.
- **A process that restarts on every pass is broken, not healed.** If the event log shows repeated `restart_failed` or backoff for the same process, stop and read its log file — do not keep restarting it.
- **Do not supervise the supervisor.** One watchdog per machine; a second copy will fight the first over the same PIDs.
- **Secrets stay out of the config.** The config file is plain JSON — pass credentials through the environment (`env` values may reference `${VAR}`), never as literals.
