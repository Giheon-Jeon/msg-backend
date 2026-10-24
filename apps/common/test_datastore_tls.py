import os
import runpy
import ssl
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from redis import ConnectionPool
from redis.connection import SSLConnection


class DatastoreTlsSettingsTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ca = str(Path(self.directory.name) / "ca.crt")
        Path(self.ca).touch()
        self.environment = {
            "DJANGO_SECRET_KEY": "tls-settings-test-only",
            "JWT_SECRET": "tls-settings-test-only-separate-key",
        }

    def load_settings(self, **environment):
        with patch.dict(os.environ, self.environment | environment, clear=True):
            with patch("dotenv.load_dotenv"):
                return runpy.run_path(
                    str(Path(__file__).resolve().parents[2] / "config" / "settings.py")
                )

    def production_environment(self):
        return {
            "DATASTORE_TLS_REQUIRED": "True",
            "POSTGRES_SSLMODE": "verify-full",
            "POSTGRES_SSLROOTCERT": self.ca,
            "REDIS_URL": "rediss://ctf_cache:test-only@10.90.10.30:6379/1",
            "REDIS_SSL_CA_CERTS": self.ca,
        }

    def test_development_defaults_remain_unchanged(self):
        settings = self.load_settings()
        self.assertNotIn("OPTIONS", settings["DATABASES"]["default"])
        self.assertEqual(settings["CACHES"]["default"]["LOCATION"], "redis://127.0.0.1:6379/1")
        self.assertNotIn("CONNECTION_POOL_KWARGS", settings["CACHES"]["default"]["OPTIONS"])

    def test_production_enables_verified_tls_and_preserves_acl_credentials(self):
        settings = self.load_settings(**self.production_environment())
        self.assertEqual(settings["DATABASES"]["default"]["OPTIONS"], {
            "sslmode": "verify-full", "sslrootcert": self.ca,
        })
        cache = settings["CACHES"]["default"]
        kwargs = cache["OPTIONS"]["CONNECTION_POOL_KWARGS"]
        self.assertEqual(kwargs, {
            "ssl_cert_reqs": "required", "ssl_ca_certs": self.ca, "ssl_check_hostname": True,
        })
        pool = ConnectionPool.from_url(cache["LOCATION"], **kwargs)
        self.addCleanup(pool.disconnect)
        self.assertIs(pool.connection_class, SSLConnection)
        self.assertEqual(pool.connection_kwargs["username"], "ctf_cache")
        self.assertEqual(pool.connection_kwargs["db"], 1)
        connection = pool.connection_class(**pool.connection_kwargs)
        self.assertEqual(connection.cert_reqs, ssl.CERT_REQUIRED)
        self.assertTrue(connection.check_hostname)

    def test_migration_import_needs_no_redis_or_external_connection(self):
        with patch("socket.socket.connect", side_effect=AssertionError("Unexpected connection")):
            settings = self.load_settings(
                POSTGRES_SSLMODE="verify-full", POSTGRES_SSLROOTCERT=self.ca,
                POSTGRES_USER="ctf_migrator", POSTGRES_DB="ctf_internal",
            )
        self.assertEqual(settings["DATABASES"]["default"]["USER"], "ctf_migrator")
        self.assertEqual(settings["DATABASES"]["default"]["NAME"], "ctf_internal")
        self.assertEqual(settings["DATABASES"]["default"]["OPTIONS"]["sslmode"], "verify-full")

    def test_rejects_incomplete_or_weakened_postgres_tls(self):
        for mode, ca in [("require", self.ca), ("disable", self.ca), ("", self.ca),
                         ("verify-full", ""), ("verify-full", self.directory.name),
                         ("verify-full", self.ca + ".missing")]:
            with self.subTest(mode=mode, ca=ca):
                with self.assertRaises(ImproperlyConfigured):
                    self.load_settings(POSTGRES_SSLMODE=mode, POSTGRES_SSLROOTCERT=ca)

    def test_production_requires_explicit_tls_for_both_datastores(self):
        for name in ["POSTGRES_SSLMODE", "POSTGRES_SSLROOTCERT", "REDIS_URL", "REDIS_SSL_CA_CERTS"]:
            environment = self.production_environment()
            del environment[name]
            with self.subTest(missing=name):
                with self.assertRaises(ImproperlyConfigured):
                    self.load_settings(**environment)

    def test_rejects_plaintext_redis_when_tls_is_configured(self):
        for url in ["redis://10.90.10.30:6379/1", "unix:///tmp/redis.sock", "rediss://"]:
            with self.subTest(url=url):
                with self.assertRaises(ImproperlyConfigured):
                    self.load_settings(REDIS_URL=url, REDIS_SSL_CA_CERTS=self.ca)

    def test_rediss_requires_existing_ca_even_without_production_flag(self):
        for ca in ["", self.ca + ".missing", self.directory.name]:
            with self.subTest(ca=ca):
                with self.assertRaises(ImproperlyConfigured):
                    self.load_settings(REDIS_URL="rediss://localhost:6379/1", REDIS_SSL_CA_CERTS=ca)

    def test_redis_url_cannot_override_tls_verification(self):
        for query in ["ssl_cert_reqs=none", "ssl_check_hostname=false", "ssl_ca_certs=/other",
                      "%73sl_check_hostname=false"]:
            environment = self.production_environment()
            environment["REDIS_URL"] += "?" + query
            with self.subTest(query=query):
                with self.assertRaises(ImproperlyConfigured):
                    self.load_settings(**environment)

    def test_invalid_url_error_does_not_expose_credentials(self):
        with self.assertRaises(ImproperlyConfigured) as error:
            self.load_settings(REDIS_URL="rediss://ctf_cache:private-value@localhost:bad/1")
        self.assertNotIn("private-value", str(error.exception))
        self.assertIsNone(error.exception.__cause__)
        self.assertTrue(error.exception.__suppress_context__)

    def test_invalid_required_flag_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            self.load_settings(DATASTORE_TLS_REQUIRED="treu")
