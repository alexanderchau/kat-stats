#!/usr/bin/env python3
"""Minimal webhook to trigger KAT Stats pipeline."""
import http.server
import json
import os
import subprocess
import threading
import time

PORT = 8766
TOKEN = "kat-stats-refresh-2026"
RUN_SH = "/Users/helm/Projects/kat-farmer/run.sh"
LOCK_DIR = "/Users/helm/Projects/kat-farmer/.run.lock"

# Track pipeline state
pipeline_lock = threading.Lock()
pipeline_running = False
last_trigger = 0


def run_sh_active():
    """True if ANY run.sh is executing — including the hourly launchd run.

    pipeline_running only knows about runs this process started, so on its own it
    happily launches a second run.sh alongside the launchd one (incident
    2026-07-26). run.sh takes .run.lock and writes its pid there; we read it.
    """
    try:
        with open(os.path.join(LOCK_DIR, "pid")) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)          # signal 0 = existence check only
        return True
    except PermissionError:
        return True              # exists, just not ours — still a live run
    except OSError:
        return False             # stale lock; run.sh will reclaim it

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global pipeline_running, last_trigger
        if self.path != "/trigger":
            self.send_error(404)
            return
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {TOKEN}":
            self.send_error(403)
            return
        with pipeline_lock:
            if pipeline_running:
                self._json(409, {"status": "already_running"})
                return
            # The hourly launchd run is invisible to pipeline_running — refuse
            # rather than race it. Callers poll /status, so reporting "running"
            # here is also what keeps them waiting for the real run to land.
            if run_sh_active():
                self._json(409, {"status": "already_running", "source": "launchd"})
                return
            # Rate limit: 5 min cooldown
            if time.time() - last_trigger < 300:
                remaining = int(300 - (time.time() - last_trigger))
                self._json(429, {"status": "cooldown", "retry_after": remaining})
                return
            pipeline_running = True
            last_trigger = time.time()
        # Run pipeline in background thread
        threading.Thread(target=self._run_pipeline, daemon=True).start()
        self._json(202, {"status": "started"})

    def do_GET(self):
        if self.path == "/status":
            # Report the launchd run too, else a poller sees running=false while a
            # pipeline is very much running and concludes the refresh finished.
            launchd = run_sh_active() and not pipeline_running
            self._json(200, {
                "running": pipeline_running or launchd,
                "source": "webhook" if pipeline_running else ("launchd" if launchd else None),
                "last_trigger": int(last_trigger) if last_trigger else None
            })
            return
        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _run_pipeline(self):
        global pipeline_running
        try:
            # holder_activity.py --refresh alone is a ~10 min pull; 600s guaranteed
            # a mid-write SIGKILL. Allow the full pipeline to finish.
            subprocess.run(["/bin/bash", RUN_SH], cwd=os.path.dirname(RUN_SH), timeout=1800)
        except Exception as e:
            print(f"Pipeline error: {e}")
        finally:
            with pipeline_lock:
                pipeline_running = False

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")

if __name__ == "__main__":
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Webhook listening on :{PORT}")
    server.serve_forever()
