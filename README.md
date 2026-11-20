# agent-process-supervisor

A config-driven process watchdog for agents. List the processes that must
stay up in a JSON config; the supervisor checks them and restarts the ones
that died, with exponential backoff so a crashing process does not spin.
It also works as a read-only health check — `--check` restarts nothing and
exits 1 if anything is dead.

Built by generalizing a personal daemon supervisor: the hardcoded process
list, the dead miners, and the machine-specific paths are gone. Everything
is config or environment variables.

## What it watches

Two process types:

- **`command`** — a command to run and keep alive. The supervisor starts
  it, tracks its PID, and restarts it when it dies. A process that exits
  within 2 seconds of starting counts as a failed start, not a running
  process.
- **`systemd`** — a systemd unit, checked with `systemctl is-active` and
  restarted with `systemctl restart`.

Optional per-process health checks: a `health_check` command (exit 0 =
healthy) and/or a `port` (TCP connect to 127.0.0.1). A running process
that fails its health check is treated as dead and restarted.

## Install

Python 3.9+ only, no dependencies. Clone and make the wrapper executable:

```sh
git clone https://github.com/manteclaw/agent-process-supervisor
cd agent-process-supervisor
chmod +x supervisor.sh supervisor.py
cp config.example.json config.json
```

Edit `config.json` to list your processes. Relative paths in the config
resolve against the directory containing the config file; `~` and
`${VAR}` are expanded.

## Usage

```sh
./supervisor.sh check    # one check-and-restart pass
./supervisor.sh status   # report only, restart nothing (exit 1 if dead)
./supervisor.sh daemon   # loop forever (interval from config)
./supervisor.sh restart  # stop all tracked processes, then one pass
./supervisor.sh stop     # stop all tracked command processes
```

Or call the Python tool directly:

```sh
python3 supervisor.py --config config.json --check    # same as status
python3 supervisor.py --config config.json --once     # same as check
python3 supervisor.py --config config.json --daemon [--interval N]
python3 supervisor.py --config config.json --dry-run  # alias for --check
python3 supervisor.py --config config.json --stop
```

## Example: supervise a real service

Config watching a web API and nginx:

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

Run it every 5 minutes from cron:

```
*/5 * * * * /path/to/agent-process-supervisor/supervisor.sh cron
```

The included example config watches a harmless dummy process
(`examples/dummy-worker.py`, which only prints a heartbeat line), so you
can try the whole loop safely:

```sh
python3 supervisor.py --config config.example.json --once   # starts the dummy
python3 supervisor.py --config config.example.json --check  # reports it running
pkill -f dummy-worker.py                                    # kill it
python3 supervisor.py --config config.example.json --once   # restarts it
python3 supervisor.py --config config.example.json --stop   # stops it
```

## How restarts are decided

- **Backoff:** after a *failed* restart, the next attempt waits
  `base_seconds * multiplier ^ consecutive_failures`, capped at
  `max_seconds` (defaults 60s / 2x / 480s). A process that ran fine and
  then died restarts immediately — backoff only punishes repeated
  failures. While waiting, the process reports `DEAD (BACKOFF)`.
- **Restart policy:** `"always"` restarts a dead process;
  `"never"` is monitor-only — the supervisor reports it dead and never
  touches it. Use `"never"` when first adding a process you are unsure
  about.
- **Kill tree:** restarting a still-running process (e.g. unhealthy)
  terminates it and its children (SIGTERM, then SIGKILL for survivors).
- **Events:** every check, start, restart, and failure is appended as
  JSONL to `<state_dir>/events.jsonl`; PIDs and failure counts live in
  `<state_dir>/state.json`. Per-process stdout/stderr goes to its
  `log_file` (default `<state_dir>/logs/<name>.log`).
- **Exit codes:** 0 when every process is healthy (or was successfully
  restarted), 1 when any is dead or a restart failed, 2 for a missing
  config. `--check` is safe to wire into any monitoring.

## Config reference

| Field | Meaning |
|---|---|
| `state_dir` | Where PIDs, failure counts, and the event log live (default `~/.local/state/agent-process-supervisor`) |
| `log_dir` | Where per-process logs go (default `<state_dir>/logs`) |
| `interval_seconds` | Daemon loop interval (default 30) |
| `backoff` | `{base_seconds, max_seconds, multiplier}` (defaults 60 / 480 / 2) |
| `processes[].name` | Unique name, used in logs and state |
| `processes[].type` | `command` or `systemd` |
| `processes[].command` | Command argv (command type) |
| `processes[].cwd` | Working directory (command type, default `.`) |
| `processes[].env` | Extra environment variables (command type) |
| `processes[].log_file` | Where stdout/stderr go (command type) |
| `processes[].unit` | Systemd unit name (systemd type) |
| `processes[].restart_policy` | `always` (default) or `never` |
| `processes[].health_check` | Command argv; exit 0 = healthy |
| `processes[].port` | TCP port that must accept connections on 127.0.0.1 |

Config location: `--config PATH`, `$APS_CONFIG`, or `./config.json`.
State directory can also be set with `$APS_STATE_DIR`. If a
`supervisor.env` file sits next to `supervisor.sh`, it is sourced before
a run so supervised processes inherit its variables (cron does not load
your shell environment).

## Agent skill

`SKILL.md` is the agent-skill version of this README: when to supervise a
process instead of restarting it by hand, and the rules (monitor-only
first, never supervise a service you do not own, a process that restarts
every pass is broken, not healed).

## License

MIT — see `LICENSE`.
