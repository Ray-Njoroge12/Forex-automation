import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

logger = logging.getLogger("health")

# Global state for health monitoring
_health_status = {
    "status": "starting",
    "last_heartbeat": None,
    "start_time": datetime.now().isoformat(),
    "version": "1.2.0-hardened",
    "account_balance": 0.0,
    "is_trading_halted": False,
}

def update_health_status(**kwargs):
    """Updates the global health status dictionary."""
    _health_status.update(kwargs)
    _health_status["last_heartbeat"] = datetime.now().isoformat()

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(_health_status).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress standard HTTP logging to keep console clean
        return

def run_health_server(port: int = 8080):
    """Runs the health check HTTP server in a blocking loop."""
    server_address = ("", port)
    try:
        httpd = HTTPServer(server_address, HealthCheckHandler)
    except OSError as exc:
        logger.warning("Health Check API disabled; bind failed on port %d: %s", port, exc)
        return
    logger.info("Health Check API started on port %d", port)
    httpd.serve_forever()

def start_health_api(port: int = 8080):
    """Starts the health check API in a background thread."""
    thread = threading.Thread(target=run_health_server, args=(port,), daemon=True)
    thread.start()
    return thread
