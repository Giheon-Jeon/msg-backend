"""Isolated database and loopback HTTP settings for the opt-in storage check."""

import os
import secrets
import uuid

os.environ["DJANGO_SECRET_KEY"] = secrets.token_urlsafe(48)
os.environ["JWT_SECRET"] = secrets.token_urlsafe(48)

from config.settings import *  # noqa: F403,E402

if DATABASES["default"]["HOST"] not in {"127.0.0.1", "localhost"}:  # noqa: F405
    raise RuntimeError("Storage verification requires a loopback test database")

DATABASES["default"]["TEST"] = {"NAME": "test_msg_gcs_smoke_" + uuid.uuid4().hex[:12]}  # noqa: F405
DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
SECURE_SSL_REDIRECT = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
RELEASE_POLL_SOURCE_REF = "refs/heads/main"
