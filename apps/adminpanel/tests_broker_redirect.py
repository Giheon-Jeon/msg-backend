import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test import SimpleTestCase, override_settings

from apps.adminpanel import resource_broker
from apps.instances.services import SchedulerError


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class BrokerRedirectTests(SimpleTestCase):
    """브로커 호출이 리다이렉트를 따라갈 때 토큰이 다른 호스트로 넘어가지 않는다 (QA TC-607)."""

    def start(self, handler):
        server = _serve(handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_redirect_to_other_host_is_not_followed(self):
        received = []

        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        other_port = self.start(Other)

        class Broker(BaseHTTPRequestHandler):
            def do_GET(self):
                # 127.0.0.1 과 localhost 는 다른 호스트 이름이다.
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{other_port}{self.path}")
                self.end_headers()

            def log_message(self, *args):
                pass

        port = self.start(Broker)
        with override_settings(
            RESOURCE_BROKER_BASE_URL=f"http://127.0.0.1:{port}",
            INVENTORY_API_TOKEN="secret-token",
        ):
            with self.assertRaises(SchedulerError):
                resource_broker._get("/v1/inventory/resource-targets", {"limit": 1})

        self.assertEqual(received, [])

    def test_redirect_within_same_host_keeps_working(self):
        received = []

        class Broker(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/old"):
                    self.send_response(301)
                    self.send_header("Location", "/api" + self.path[len("/old"):])
                    self.end_headers()
                    return
                received.append(self.headers.get("Authorization"))
                body = json.dumps({"items": [], "total": 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        port = self.start(Broker)
        with override_settings(
            RESOURCE_BROKER_BASE_URL=f"http://127.0.0.1:{port}/old",
            INVENTORY_API_TOKEN="secret-token",
        ):
            page = resource_broker._get("/v1/inventory/resource-targets", {"limit": 1})

        self.assertEqual(page, {"items": [], "total": 0})
        self.assertEqual(received, ["Bearer secret-token"])
