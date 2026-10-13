import importlib.util
import io
import json
import os
import threading
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from .services import ScoreFetchError, _request_scores


@contextmanager
def score_server(status=200, body=b'{"code":"SUCCESS","data":null}', location=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@patch.dict(os.environ, {"TEST_SCORE_HTTP_TOKEN": "test-only-score-token"})
class KothHttpClientTests(SimpleTestCase):
    period = datetime(2026, 9, 10, 0, 15, tzinfo=timezone.utc)

    def challenge(self, url):
        return SimpleNamespace(
            score_api_url=url, score_api_token_env="TEST_SCORE_HTTP_TOKEN"
        )

    def test_score_request_preserves_query_and_sends_period_and_token(self):
        with score_server() as (url, requests):
            payload = _request_scores(
                self.challenge(url + "/scores?mode=koth&period_id=old&scored_at=old"),
                self.period,
            )
        self.assertEqual(payload, {"code": "SUCCESS", "data": None})
        self.assertEqual(len(requests), 1)
        path, headers = requests[0]
        self.assertEqual(urlsplit(path).path, "/scores")
        self.assertEqual(
            parse_qs(urlsplit(path).query),
            {
                "mode": ["koth"],
                "period_id": ["2026-09-10T00:15:00Z"],
                "scored_at": ["2026-09-10T00:15:00Z"],
            },
        )
        self.assertEqual(headers["X-KOTH-Internal-Token"], "test-only-score-token")

    def test_score_redirect_does_not_forward_internal_token(self):
        with score_server() as (target, target_requests):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status), score_server(
                    status, location=target
                ) as (url, requests):
                    with self.assertRaisesRegex(ScoreFetchError, f"HTTP {status}"):
                        _request_scores(self.challenge(url + "/scores"), self.period)
                    self.assertEqual(len(requests), 1)
            self.assertEqual(target_requests, [])

    def test_score_request_rejects_invalid_urls_before_connecting(self):
        for url in (
            "file:///not-a-score-endpoint",
            "ftp://example.test/scores",
            "http:///scores",
            "http://example.test:invalid/scores",
            "http://example.test:70000/scores",
            "http://[invalid/scores",
            "https://user:password@example.test/scores",
            "https://example.test/scores#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ScoreFetchError):
                _request_scores(self.challenge(url), self.period)

    def test_score_request_rejects_bad_status_and_invalid_payloads(self):
        for status, body in (
            (503, b"unavailable"),
            (200, b"not-json"),
            (200, b"\xff"),
            (200, b"[]"),
            (200, b'{"code":"SUCCESS","data":[]}'),
        ):
            with self.subTest(status=status, body=body), score_server(status, body) as (
                url,
                _,
            ):
                with self.assertRaises(ScoreFetchError):
                    _request_scores(self.challenge(url), self.period)

    def test_https_request_uses_tls_connection_and_closes_it(self):
        with patch("apps.koth.services.http.client.HTTPSConnection") as factory:
            connection = factory.return_value
            response = connection.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"code":"SUCCESS","data":null}'
            _request_scores(
                self.challenge("https://scores.example.test:8443/scores"), self.period
            )
        factory.assert_called_once_with("scores.example.test", 8443, timeout=10)
        connection.close.assert_called_once_with()


class KothTemplateCheckerTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = (
            Path(__file__).resolve().parents[2]
            / "koth-template/prob/for_organizer/checker/checker.py"
        )
        spec = importlib.util.spec_from_file_location("koth_template_checker", path)
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)

    def check(self, url):
        output = io.StringIO()
        parsed = urlsplit(url)
        with patch.dict(
            os.environ,
            {
                "TARGET_HOST": parsed.hostname,
                "TARGET_PORT": str(parsed.port),
                "TEAM_ID": "test-team",
                "KOTH_CHALLENGE_ID": "test-challenge",
            },
        ), redirect_stdout(output):
            self.checker.main()
        return json.loads(output.getvalue())["metric_score"]

    def test_checker_accepts_direct_success_and_rejects_failure(self):
        for status, expected in ((200, 100), (503, 0)):
            with self.subTest(status=status), score_server(status) as (url, _):
                self.assertEqual(self.check(url), expected)

    def test_checker_rejects_redirect_without_contacting_target(self):
        with score_server() as (target, target_requests):
            with score_server(302, location=target) as (url, _):
                self.assertEqual(self.check(url), 0)
            self.assertEqual(target_requests, [])

    def test_checker_rejects_invalid_hosts_and_ports(self):
        for host, port in (
            ("example.test/path", "80"),
            ("user@example.test", "80"),
            ("127.0.0.1", "0"),
            ("127.0.0.1", "65536"),
        ):
            with self.subTest(host=host, port=port):
                self.assertIsNone(self.checker.target_url(host, port))
