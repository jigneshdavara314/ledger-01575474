"""
Tiny local web server for the dashboard — with action BUTTONS.

    python serve.py

Then open  http://localhost:8755  in your browser.

The page shows your live portfolio and has buttons:
  - Resolve   -> settles finished games (run.py resolve)
  - Longshot  -> places longshot-fade bets (run.py longshot)
  - Lag arb   -> checks resolution-lag opportunities (run.py lagwatch)

Each button runs the real bot command and shows the output, then refreshes.
PAPER mode only — no real money. Ctrl+C to stop.
"""
import sys
import subprocess
import http.server
import socketserver

PORT = 8755

# Only these actions can be triggered from the browser (whitelist for safety).
ALLOWED_ACTIONS = {"resolve", "longshot", "lagwatch", "short", "scout", "backtest"}

PYTHON = sys.executable  # the same interpreter running this server


class DashboardHandler(http.server.SimpleHTTPRequestHandler):

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
            try:
                # fresh import each load so code edits show up without restart
                import importlib, dashboard
                importlib.reload(dashboard)
                html = dashboard.build_html()
            except Exception as e:
                html = f"<h1>Dashboard error</h1><pre>{e}</pre>"
            self._send(200, html)
        else:
            self.send_error(404)

    def do_POST(self):
        # Adjust the daily investment budget (persists to settings.json).
        if self.path.startswith("/set-budget/"):
            raw = self.path[len("/set-budget/"):].strip("/")
            try:
                from polybot.settings import set_daily_budget
                newval = set_daily_budget(float(raw))
                # regenerate the dashboard so the new value shows immediately
                subprocess.run([PYTHON, "dashboard.py"], capture_output=True, timeout=60)
                self._send(200, f"Daily budget set to ${newval:,.2f}.", "text/plain")
            except Exception as e:
                self._send(400, f"Bad budget value: {e}", "text/plain")
            return
        if not self.path.startswith("/run/"):
            self.send_error(404)
            return
        action = self.path[len("/run/"):].strip("/")
        if action not in ALLOWED_ACTIONS:
            self._send(400, f"Action '{action}' is not allowed.", "text/plain")
            return
        try:
            if action == "backtest":
                cmd = [PYTHON, "run_strategy_backtest.py", "30"]
                tmo = 600
            else:
                cmd = [PYTHON, "run.py", action]
                tmo = 180
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=tmo,
            )
            out = proc.stdout or ""
            err = proc.stderr or ""
            # After any trading/resolving action, regenerate the dashboard file too
            subprocess.run([PYTHON, "dashboard.py"], capture_output=True, timeout=60)
            body = out
            if err.strip():
                body += "\n[stderr]\n" + err
            self._send(200, body, "text/plain; charset=utf-8")
        except subprocess.TimeoutExpired:
            self._send(200, f"'{action}' timed out (still may have partially run).",
                       "text/plain")
        except Exception as e:
            self._send(500, f"Error running '{action}': {e}", "text/plain")

    def log_message(self, *args):
        pass  # quiet


def main():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), DashboardHandler) as httpd:
        print("=" * 52)
        print("  Polymarket bot dashboard is LIVE at:")
        print(f"  ->  http://localhost:{PORT}")
        print("=" * 52)
        print("  Buttons: Resolve / Longshot / Lag arb")
        print("  Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
