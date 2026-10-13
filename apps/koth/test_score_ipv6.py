import http.client
import os
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.core.validators import URLValidator
from django.test import SimpleTestCase, TestCase

from .models import (
    KothChallenge,
    KothChallengeStatus,
    KothClub,
    KothScorePeriod,
    KothScorePeriodStatus,
)
from .services import ScoreFetchError, _request_scores


@patch.dict(os.environ, {"TEST_IPV6_SCORE_TOKEN": "test-only-score-token"})
class IPv6ScoreURLTests(SimpleTestCase):
    period = datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)

    def assert_connection_target(self, url, expected_host, expected_port):
        URLValidator(schemes=["http", "https"])(url)
        challenge = KothChallenge(
            score_api_url=url, score_api_token_env="TEST_IPV6_SCORE_TOKEN"
        )
        targets = []

        def stop_before_network(connection):
            targets.append((connection.host, connection.port))
            raise OSError("test connection unavailable")

        # Keep the real constructor and request path: mocking the constructor
        # would hide http.client's host/port parsing regression.
        with patch.object(http.client.HTTPConnection, "connect", stop_before_network):
            with patch.object(
                http.client.HTTPSConnection, "connect", stop_before_network
            ):
                with self.assertRaisesRegex(
                    ScoreFetchError, "test connection unavailable"
                ):
                    _request_scores(challenge, self.period)
        self.assertEqual(targets, [(expected_host, expected_port)])

    def test_http_ipv6_uses_default_port(self):
        for host in ("2001:db8::10", "2001:db8::abcd", "::1"):
            with self.subTest(host=host):
                self.assert_connection_target(f"http://[{host}]/scores", host, 80)

    def test_https_ipv6_uses_default_port(self):
        for host in ("2001:db8::10", "2001:db8::abcd", "::1"):
            with self.subTest(host=host):
                self.assert_connection_target(f"https://[{host}]/scores", host, 443)

    def test_ipv6_preserves_explicit_ports(self):
        for scheme, port in (("http", 8080), ("https", 8443)):
            for host in ("2001:db8::10", "2001:db8::abcd"):
                with self.subTest(scheme=scheme, host=host):
                    self.assert_connection_target(
                        f"{scheme}://[{host}]:{port}/scores", host, port
                    )

    def test_connection_creation_errors_become_score_fetch_errors(self):
        for scheme, connection_class in (
            ("http", "HTTPConnection"),
            ("https", "HTTPSConnection"),
        ):
            challenge = KothChallenge(
                score_api_url=f"{scheme}://[2001:db8::abcd]/scores",
                score_api_token_env="TEST_IPV6_SCORE_TOKEN",
            )
            with self.subTest(scheme=scheme):
                with patch(
                    f"apps.koth.services.http.client.{connection_class}",
                    side_effect=http.client.InvalidURL("test invalid connection"),
                ):
                    with self.assertRaisesRegex(
                        ScoreFetchError, "test invalid connection"
                    ):
                        _request_scores(challenge, self.period)


@patch.dict(os.environ, {"TEST_IPV6_SCORE_TOKEN": "test-only-score-token"})
class IPv6ScoreBatchTests(TestCase):
    def test_failed_ipv6_endpoint_is_retried_without_skipping_next_challenge(self):
        challenges = []
        for index, url in enumerate(
            ("http://[2001:db8::abcd]/scores", "http://example.test/scores"), 1
        ):
            challenge = KothChallenge(
                club=KothClub.objects.create(name=f"ipv6-club-{index}"),
                title=f"ipv6-{index}",
                open_group=index,
                status=KothChallengeStatus.ACTIVE,
                inbound_internal_token_hash=str(index) * 64,
                score_api_url=url,
                score_api_token_env="TEST_IPV6_SCORE_TOKEN",
            )
            challenge.full_clean()
            challenge.save()
            challenges.append(challenge)
        calls, targets = [], []

        def request_or_return_empty(challenge, period):
            calls.append(challenge.pk)
            if challenge.pk == challenges[0].pk:
                return _request_scores(challenge, period)
            return {"code": "SUCCESS", "data": None}

        def stop_before_network(connection):
            targets.append((connection.host, connection.port))
            raise OSError("test IPv6 endpoint unavailable")

        with patch(
            "apps.koth.services._request_scores", side_effect=request_or_return_empty
        ):
            with patch.object(
                http.client.HTTPConnection, "connect", stop_before_network
            ):
                with patch(
                    "apps.koth.management.commands.poll_koth_scores.time.sleep"
                ) as sleep:
                    with self.assertRaises(CommandError):
                        call_command(
                            "poll_koth_scores",
                            period_id="2026-09-10T03:00:00Z",
                            max_retries=1,
                            stdout=StringIO(),
                            stderr=StringIO(),
                        )
        self.assertEqual(calls, [challenges[0].pk, challenges[0].pk, challenges[1].pk])
        self.assertEqual(targets, [("2001:db8::abcd", 80)] * 2)
        sleep.assert_called_once_with(60)
        failed = KothScorePeriod.objects.get(challenge=challenges[0])
        self.assertEqual(failed.status, KothScorePeriodStatus.FAILED)
        self.assertEqual(failed.attempts, 2)
        self.assertIn("test IPv6 endpoint unavailable", failed.last_error)
        applied = KothScorePeriod.objects.get(challenge=challenges[1])
        self.assertEqual(applied.status, KothScorePeriodStatus.APPLIED)
        self.assertEqual(applied.attempts, 1)
