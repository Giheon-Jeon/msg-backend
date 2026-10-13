"""Disposable Django request worker for the process-restart regression tests."""

import json
import os
import sys
from contextlib import nullcontext
from unittest.mock import patch


CRASH_EXIT_CODE = 73


def main():
    configuration = json.load(sys.stdin)

    import django
    from django.conf import settings

    # Use the parent's active test database, never the database from .env.
    settings.DATABASES = {"default": configuration["database"]}
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    }
    settings.ALLOWED_HOSTS = ["testserver"]
    settings.SECURE_SSL_REDIRECT = False
    django.setup()

    from django.db import connections
    from rest_framework.renderers import JSONRenderer
    from rest_framework.test import APIClient

    from apps.accounts.models import User

    def emit_response(response):
        print(
            json.dumps(
                {
                    "status": response.status_code,
                    "body": json.loads(JSONRenderer().render(response.data)),
                }
            ),
            flush=True,
        )

    def crash_before_commit(response):
        emit_response(response)
        # Bypass exception handling and connection cleanup, as a killed worker does.
        os._exit(CRASH_EXIT_CODE)

    def crash_after_commit(cache_key, response, fingerprint):
        emit_response(response)
        os._exit(CRASH_EXIT_CODE)

    mode = configuration["mode"]
    if mode == "before_commit":
        crash = patch(
            "apps.board.idempotency._normalize_response_body",
            side_effect=crash_before_commit,
        )
    elif mode == "after_commit":
        crash = patch(
            "apps.board.idempotency._cache_set",
            side_effect=crash_after_commit,
        )
    else:
        crash = nullcontext()

    client = APIClient()
    client.force_authenticate(user=User.objects.get(pk=configuration["user_id"]))
    with crash, patch("apps.board.services.random.randint", return_value=1):
        response = client.post(
            "/api/v1/board/dice/roll",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY=configuration["key"],
        )
    emit_response(response)
    connections.close_all()


if __name__ == "__main__":
    main()
