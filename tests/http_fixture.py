"""Local deterministic HTTP fixture, for protocol testing only."""
import json
import socket
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.content_agent.models import mock


@contextmanager
def model_endpoint(fault=None):
    observations = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(size))
            ctx = json.loads(payload["messages"][-1]["content"])
            event = {"path":self.path, "model":payload["model"],
                     "incoming_observation":ctx}
            observations.append(event)
            # Produce one successful tool action before the transport fault.
            if fault and len(observations) == 2:
                event["fixture_fault"] = fault
                if fault == "http_503":
                    self.send_error(503)
                    return
                self.send_response(200)
                self.send_header("Content-Length", "1000" if fault == "disconnect" else "2")
                self.end_headers()
                self.wfile.write(b"{}")
                self.wfile.flush()
                self.close_connection = True
                return
            action = mock(ctx)
            event["fixture_decision"] = action
            body = json.dumps({"choices":[{"message":{"content":json.dumps(action, ensure_ascii=False)}}]},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            with socket.create_connection(server.server_address, timeout=2):
                pass
        except OSError as exc:
            if exc.errno not in (1, 13) and getattr(exc, "winerror", None) != 10013:
                raise
            raise unittest.SkipTest(f"本机 TCP 不可用（errno={exc.errno}，winerror={getattr(exc, 'winerror', None)}）") from exc
        yield f"http://127.0.0.1:{server.server_port}/v1", observations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
