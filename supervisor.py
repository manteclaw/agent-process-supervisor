#!/usr/bin/env python3
"""
agent-process-supervisor — config-driven process watchdog.

Watches the processes listed in a JSON config file and restarts the ones
that died, with exponential backoff so a crashing process does not spin.
Two process types are supported:

  "command" — a command to run and keep alive (PID tracked in state).
  "systemd" — a systemd unit, checked with `systemctl is-active` and
               restarted with `systemctl restart`.

All paths come from the config file or environment variables. Relative
paths in the config are resolved against the directory containing the
config file, so a config is portable. No paths are hardcoded.

Usage:
    python3 supervisor.py --config config.json --check     # report only
    python3 supervisor.py --config config.json --once      # one heal pass
    python3 supervisor.py --config config.json --daemon    # loop forever
    python3 supervisor.py --config config.json --stop      # stop tracked processes

    --check and --dry-run are the same: report status, restart nothing.
    Exit code is 0 when every process is healthy, 1 when any is dead
    (or a restart failed), so the tool works as a monitoring check.

Config location (first match wins):
    --config PATH | $APS_CONFIG | ./config.json
State directory (first match wins):
    config "state_dir" | $APS_STATE_DIR | ~/.local/state/agent-process-supervisor
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BACKOFF = {"base_seconds": 60, "max_seconds": 480, "multiplier": 2}
DEFAULT_INTERVAL_SECONDS = 30


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expand(value: str) -> str:
    """Expand ~ and $VARS in a path string."""
    return os.path.expanduser(os.path.expandvars(value))


class Supervisor:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config_dir = config_path.parent.resolve()
        self.config = json.loads(config_path.read_text())
        self.state_dir = self._resolve_path(
            self.config.get("state_dir")
            or os.environ.get("APS_STATE_DIR")
            or "~/.local/state/agent-process-supervisor"
        )
        self.log_dir = self._resolve_path(
            self.config.get("log_dir") or str(self.state_dir / "logs")
        )
        self.state_file = self.state_dir / "state.json"
        self.events_file = self.state_dir / "events.jsonl"
        backoff = {**DEFAULT_BACKOFF, **self.config.get("backoff", {})}
        self.backoff_base = int(backoff["base_seconds"])
        self.backoff_max = int(backoff["max_seconds"])
        self.backoff_multiplier = int(backoff["multiplier"])
        self.interval = int(
            self.config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
        )
        self.processes = self.config.get("processes", [])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ── paths ──────────────────────────────────────────────────────
    def _resolve_path(self, value: str) -> Path:
        """Resolve a config path: absolute as-is, relative against config dir."""
        p = Path(expand(value))
        if not p.is_absolute():
            p = self.config_dir / p
        return p

    # ── state / events ─────────────────────────────────────────────
    def load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def save_state(self, state: dict):
        self.state_file.write_text(json.dumps(state, indent=2))

    def log_event(self, event: dict):
        with open(self.events_file, "a") as f:
            f.write(json.dumps({**event, "timestamp": now_iso()}) + "\n")

    # ── process helpers ────────────────────────────────────────────
    @staticmethod
    def is_alive(pid) -> bool:
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def find_children(parent_pid: int) -> list:
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(parent_pid)], text=True,
                stderr=subprocess.DEVNULL,
            )
            return [int(l) for l in out.split() if l.strip().isdigit()]
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

    def kill_tree(self, pid: int):
        """Terminate a process and its children, SIGKILLing survivors."""
        children = self.find_children(pid)
        for p in children + [pid]:
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(1)
        for p in children + [pid]:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass

    def port_open(self, port: int, host: str = "127.0.0.1") -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=3):
                return True
        except OSError:
            return False

    def run_health_check(self, proc: dict) -> bool:
        """Run the optional health-check command; exit 0 means healthy."""
        cmd = proc.get("health_check")
        if not cmd:
            return True
        try:
            result = subprocess.run(
                cmd, cwd=self._cwd(proc), capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _cwd(self, proc: dict) -> str:
        return str(self._resolve_path(proc.get("cwd", ".")))

    # ── backoff ────────────────────────────────────────────────────
    def backoff_seconds(self, state: dict, name: str) -> int:
        failures = state.get(name, {}).get("consecutive_failures", 0)
        secs = self.backoff_base * (self.backoff_multiplier ** failures)
        return min(secs, self.backoff_max)

    def can_attempt_restart(self, state: dict, name: str) -> bool:
        """Backoff only gates restarts after a *failed* attempt.

        A process that ran fine and then died restarts immediately; a
        process whose last restart failed waits base * multiplier ^
        consecutive_failures (capped at max). Without this, a healthy
        process that dies once would sit out a full backoff window
        for no reason.
        """
        failures = state.get(name, {}).get("consecutive_failures", 0)
        if failures == 0:
            return True
        last = state.get(name, {}).get("last_attempt")
        if not last:
            return True
        elapsed = (
            datetime.now(timezone.utc) - datetime.fromisoformat(last)
        ).total_seconds()
        return elapsed >= self.backoff_seconds(state, name)

    def record_attempt(self, state: dict, name: str, success: bool):
        entry = state.setdefault(name, {})
        entry["last_attempt"] = now_iso()
        entry["consecutive_failures"] = (
            0 if success else entry.get("consecutive_failures", 0) + 1
        )

    # ── command processes ──────────────────────────────────────────
    def start_command(self, proc: dict) -> int | None:
        name = proc["name"]
        cmd = proc["command"]
        log_path = self._resolve_path(
            proc.get("log_file") or f"logs/{name}.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update({k: expand(str(v)) for k, v in proc.get("env", {}).items()})
        log_f = open(log_path, "a")
        log_f.write(f"\n[{now_iso()}] supervisor start: {name}\n")
        log_f.flush()
        try:
            child = subprocess.Popen(
                cmd, cwd=self._cwd(proc), stdout=log_f,
                stderr=subprocess.STDOUT, env=env, start_new_session=True,
            )
        except OSError as e:
            log_f.write(f"failed to start: {e}\n")
            log_f.close()
            self.log_event({
                "event": "start_failed", "process": name,
                "message": str(e),
            })
            return None
        # Fail-fast check: a process that exits within 2s is broken, not running.
        time.sleep(2)
        if child.poll() is not None:
            self.log_event({
                "event": "start_failed", "process": name,
                "returncode": child.returncode,
                "message": f"exited immediately with code {child.returncode}",
            })
            log_f.close()
            return None
        self.log_event({
            "event": "started", "process": name, "pid": child.pid,
            "command": " ".join(str(c) for c in cmd),
        })
        return child.pid

    def check_command(self, proc: dict, state: dict, heal: bool) -> dict:
        """Return a status dict for one command process; restart it if dead."""
        name = proc["name"]
        pid = state.get(name, {}).get("pid")
        alive = self.is_alive(pid)
        status = {"name": name, "type": "command", "pid": pid}
        if alive:
            healthy = self.run_health_check(proc)
            if proc.get("port") and not self.port_open(proc["port"]):
                healthy = False
            status["status"] = "running" if healthy else "unhealthy"
            if not healthy:
                self.log_event({
                    "event": "unhealthy", "process": name, "pid": pid,
                })
            if heal and not healthy:
                return self._restart_command(proc, state, status, pid)
            return status
        status["status"] = "dead"
        self.log_event({"event": "dead", "process": name, "pid": pid})
        if not heal:
            return status
        if proc.get("restart_policy", "always") == "never":
            self.log_event({
                "event": "restart_skipped", "process": name,
                "message": "restart_policy is 'never' (monitor only)",
            })
            return status
        if not self.can_attempt_restart(state, name):
            wait = self.backoff_seconds(state, name)
            status["status"] = "dead (backoff)"
            status["backoff_seconds"] = wait
            self.log_event({
                "event": "backoff", "process": name,
                "backoff_seconds": wait,
            })
            return status
        return self._restart_command(proc, state, status, pid)

    def _restart_command(self, proc, state, status, old_pid):
        name = proc["name"]
        if old_pid and self.is_alive(old_pid):
            self.kill_tree(old_pid)
            time.sleep(1)
        new_pid = self.start_command(proc)
        success = new_pid is not None and self.is_alive(new_pid)
        self.record_attempt(state, name, success)
        if success:
            state.setdefault(name, {})["pid"] = new_pid
            status.update({"status": "restarted", "pid": new_pid,
                           "old_pid": old_pid})
            self.log_event({
                "event": "restarted", "process": name,
                "old_pid": old_pid, "new_pid": new_pid,
            })
        else:
            state.setdefault(name, {})["pid"] = None
            status.update({"status": "restart_failed", "old_pid": old_pid})
            self.log_event({
                "event": "restart_failed", "process": name,
                "old_pid": old_pid,
            })
        return status

    # ── systemd processes ──────────────────────────────────────────
    def check_systemd(self, proc: dict, state: dict, heal: bool) -> dict:
        name = proc["name"]
        unit = proc["unit"]
        status = {"name": name, "type": "systemd", "unit": unit}
        try:
            result = subprocess.run(
                ["systemctl", "is-active", unit],
                capture_output=True, text=True, timeout=10,
            )
            active = result.returncode == 0
            status["status"] = (
                "running" if active else f"dead ({result.stdout.strip()})"
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            active = False
            status["status"] = f"dead (systemctl error: {e})"
        if active:
            if proc.get("port") and not self.port_open(proc["port"]):
                status["status"] = "unhealthy"
            return status
        self.log_event({"event": "dead", "process": name, "unit": unit})
        if not heal:
            return status
        if proc.get("restart_policy", "always") == "never":
            self.log_event({
                "event": "restart_skipped", "process": name,
                "message": "restart_policy is 'never' (monitor only)",
            })
            return status
        if not self.can_attempt_restart(state, name):
            wait = self.backoff_seconds(state, name)
            status["status"] = "dead (backoff)"
            status["backoff_seconds"] = wait
            self.log_event({
                "event": "backoff", "process": name,
                "backoff_seconds": wait,
            })
            return status
        try:
            result = subprocess.run(
                ["systemctl", "restart", unit],
                capture_output=True, text=True, timeout=30,
            )
            success = result.returncode == 0
            detail = result.stderr.strip() or result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            success, detail = False, str(e)
        self.record_attempt(state, name, success)
        status["status"] = "restarted" if success else "restart_failed"
        self.log_event({
            "event": status["status"], "process": name, "unit": unit,
            "detail": detail[:200],
        })
        return status

    # ── top level ──────────────────────────────────────────────────
    def run_check(self, heal: bool) -> list:
        state = self.load_state()
        results = []
        for proc in self.processes:
            if proc["type"] == "command":
                results.append(self.check_command(proc, state, heal))
            elif proc["type"] == "systemd":
                results.append(self.check_systemd(proc, state, heal))
            else:
                results.append({
                    "name": proc.get("name", "?"), "type": proc.get("type"),
                    "status": f"unknown type '{proc.get('type')}'",
                })
        self.save_state(state)
        return results

    def stop_all(self):
        """Stop every tracked command process (systemd units are untouched)."""
        state = self.load_state()
        stopped = []
        for proc in self.processes:
            if proc["type"] != "command":
                continue
            name = proc["name"]
            pid = state.get(name, {}).get("pid")
            if pid and self.is_alive(pid):
                self.kill_tree(pid)
                stopped.append((name, pid))
                self.log_event({"event": "stopped", "process": name, "pid": pid})
            state.setdefault(name, {})["pid"] = None
        self.save_state(state)
        return stopped


def print_results(results: list):
    for r in results:
        pid = f" (PID {r['pid']})" if r.get("pid") else ""
        extra = ""
        if r.get("backoff_seconds"):
            extra = f" — retry in {r['backoff_seconds']}s"
        print(f"  {r['status'].upper():<18} {r['name']}{pid}{extra}")
    healthy = all(r["status"] in ("running", "restarted") for r in results)
    print(f"\n{sum(1 for r in results if r['status'] in ('running', 'restarted'))}"
          f"/{len(results)} healthy")
    return 0 if healthy else 1


def find_config(explicit: str | None) -> Path:
    if explicit:
        return Path(expand(explicit))
    if os.environ.get("APS_CONFIG"):
        return Path(expand(os.environ["APS_CONFIG"]))
    return Path("config.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Config-driven process watchdog: restart dead "
                    "processes, with backoff. --check reports only."
    )
    parser.add_argument("--config", help="path to config JSON")
    parser.add_argument("--check", action="store_true",
                        help="report status, restart nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="same as --check")
    parser.add_argument("--once", action="store_true",
                        help="one check-and-restart pass, then exit")
    parser.add_argument("--daemon", action="store_true",
                        help="loop forever (interval from config)")
    parser.add_argument("--interval", type=int, default=None,
                        help="daemon loop interval in seconds")
    parser.add_argument("--stop", action="store_true",
                        help="stop all tracked command processes")
    args = parser.parse_args()

    config_path = find_config(args.config)
    if not config_path.exists():
        print(f"config not found: {config_path}", file=sys.stderr)
        print("Copy config.example.json to config.json and edit it, "
              "or pass --config.", file=sys.stderr)
        return 2
    sup = Supervisor(config_path)

    if args.stop:
        stopped = sup.stop_all()
        if not stopped:
            print("nothing running to stop")
        for name, pid in stopped:
            print(f"stopped {name} (PID {pid})")
        return 0

    if args.check or args.dry_run:
        print(f"status (config: {config_path}, no restarts):")
        return print_results(sup.run_check(heal=False))

    if args.daemon:
        interval = args.interval or sup.interval
        print(f"supervisor daemon: checking every {interval}s "
              f"(Ctrl-C to stop)")
        try:
            while True:
                sup.run_check(heal=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nsupervisor stopped")
            return 0

    # Default / --once: one healing pass.
    print(f"check + restart pass (config: {config_path}):")
    return print_results(sup.run_check(heal=True))


if __name__ == "__main__":
    sys.exit(main())
