import os
from pathlib import Path
import runpy
from unittest import TestCase
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django_redis.pool import ConnectionFactory


class RedisSettingsTests(TestCase):
    def load_settings(self, **environment):
        base = {
            "DJANGO_SECRET_KEY": "redis-settings-test-only",
            "JWT_SECRET": "redis-settings-test-only-separate-key",
        }
        with patch.dict(os.environ, base | environment, clear=True):
            with patch("dotenv.load_dotenv"), patch(
                "socket.socket.connect", side_effect=AssertionError("Unexpected connection")
            ):
                return runpy.run_path(
                    str(Path(__file__).resolve().parents[2] / "config" / "settings.py")
                )

    def check_pool(self, settings, connect_timeout, socket_timeout):
        cache = settings["CACHES"]["default"]
        options = cache["OPTIONS"]
        self.assertEqual(options["SOCKET_CONNECT_TIMEOUT"], connect_timeout)
        self.assertEqual(options["SOCKET_TIMEOUT"], socket_timeout)
        factory = ConnectionFactory(options)
        pool = factory.get_connection_pool(factory.make_connection_params(cache["LOCATION"]))
        self.addCleanup(pool.disconnect)
        connection = pool.connection_class(**pool.connection_kwargs)
        self.assertEqual(connection.socket_connect_timeout, connect_timeout)
        self.assertEqual(connection.socket_timeout, socket_timeout)
        self.assertEqual(connection.retry.get_retries(), 0)

    def test_default_timeouts_reach_pool_without_automatic_retries(self):
        self.check_pool(self.load_settings(), 1.0, 1.0)

    def test_custom_fractional_timeouts_reach_pool(self):
        self.check_pool(self.load_settings(
            REDIS_CONNECT_TIMEOUT_SECONDS="0.25", REDIS_SOCKET_TIMEOUT_SECONDS="1.5",
        ), 0.25, 1.5)

    def test_invalid_timeouts_fail_at_startup(self):
        for name in ["REDIS_CONNECT_TIMEOUT_SECONDS", "REDIS_SOCKET_TIMEOUT_SECONDS"]:
            for value in ["", "abc", "0", "-1", "nan", "inf", "-inf", "1e999"]:
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ImproperlyConfigured, name):
                        self.load_settings(**{name: value})

    def test_url_cannot_override_timeout_or_retry_policy(self):
        for query in ["socket_timeout=99", "socket_connect_timeout=0", "retry_on_timeout=true",
                      "retry_on_error=TimeoutError", "retry=anything", "%73ocket_timeout=0"]:
            with self.subTest(query=query):
                with self.assertRaises(ImproperlyConfigured):
                    self.load_settings(REDIS_URL="redis://localhost:6379/1?" + query)

    def test_other_url_options_and_acl_credentials_remain_supported(self):
        settings = self.load_settings(REDIS_URL="redis://ctf_cache:test-only@localhost:6379/1?db=2")
        self.check_pool(settings, 1.0, 1.0)

    def test_invalid_url_does_not_expose_credentials(self):
        with self.assertRaises(ImproperlyConfigured) as error:
            self.load_settings(REDIS_URL="redis://ctf_cache:private-value@[invalid/1")
        self.assertNotIn("private-value", str(error.exception))
