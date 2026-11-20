#!/usr/bin/env python3
"""
dummy-worker.py — a harmless example process for agent-process-supervisor.

It does nothing but print a heartbeat line every few seconds and wait to
be killed. It exists so you can try the supervisor end to end without
pointing it at a real service:

    cp config.example.json config.json
    python3 supervisor.py --config config.json --once    # starts the dummy
    python3 supervisor.py --config config.json --check   # reports it running
    pkill -f dummy-worker.py                             # kill it
    python3 supervisor.py --config config.json --once    # restarts it

Handles SIGTERM/SIGINT cleanly so the supervisor's kill-tree works.
"""
import os
import signal
import sys
import time
from datetime import datetime, timezone

running = True


def _stop(signum, frame):
    global running
    print(f"[{datetime.now(timezone.utc).isoformat()}] dummy-worker: "
          f"got signal {signum}, shutting down", flush=True)
    running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

print(f"[{datetime.now(timezone.utc).isoformat()}] dummy-worker: started "
      f"(PID {os.getpid()})", flush=True)
while running:
    print(f"[{datetime.now(timezone.utc).isoformat()}] dummy-worker: "
          f"heartbeat", flush=True)
    time.sleep(5)
print("dummy-worker: stopped", flush=True)
sys.exit(0)
