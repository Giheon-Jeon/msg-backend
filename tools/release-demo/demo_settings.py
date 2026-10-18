import os
from pathlib import Path

# 데모 전용 기본값. 실서비스 값과 무관한 로컬 시연용 키라 환경변수 없이 바로 뜬다.
os.environ.setdefault("DJANGO_SECRET_KEY", "release-demo-django-key-do-not-use-in-production")
os.environ.setdefault("JWT_SECRET", "release-demo-jwt-key-do-not-use-in-production")
os.environ.setdefault("DJANGO_DEBUG", "True")
os.environ.setdefault("SCHEDULER_BASE_URL", "http://127.0.0.1:8001")

from config.settings import *  # noqa: E402,F401,F403

DATA_DIR = Path(__file__).resolve().parent / ".data"
DATA_DIR.mkdir(exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DATA_DIR / "demo.sqlite3"),
    }
}
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
}
