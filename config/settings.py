import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# 기본값을 두지 않도록 변경
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY 환경변수가 필요합니다.\n"
        '  생성: python -c "import secrets; print(secrets.token_urlsafe(50))"'
    )

# SECURITY WARNING: don't run with debug turned on in production!
# 기본값 False, True 가 기본이면 환경변수 누락 시 실서버에서도 디버그 모드로 뜨게됨.
DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

SECURE_SSL_REDIRECT = not DEBUG
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.timer",
    "apps.accounts",
    "apps.adminpanel",
    "apps.board",
    "apps.common",
    "apps.teams",
    "apps.ranking",
    "apps.challenge",
    "apps.instances",
    "apps.leaderboard",
    "apps.koth",
    "apps.signature",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "msg_backend"),
        "USER": os.getenv("POSTGRES_USER", "postgres"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        # Reuse one connection per WSGI worker instead of reconnecting on every request.
        "CONN_MAX_AGE": int(os.getenv("POSTGRES_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
}

# Cache
# https://docs.djangoproject.com/en/5.2/topics/cache/

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": os.getenv("REDIS_URL", "redis://127.0.0.1:6379/1"),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}
# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = "static/"
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

GCS_ENABLED = os.getenv("GCS_ENABLED", "False").lower() == "true"
if GCS_ENABLED:
    GCS_PROJECT_ID = os.getenv("GCS_PROJECT_ID", "").strip()
    GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "").strip()
    GCS_OBJECT_PREFIX = os.getenv("GCS_OBJECT_PREFIX", "").strip("/")
    missing = [
        name
        for name, value in (
            ("GCS_PROJECT_ID", GCS_PROJECT_ID),
            ("GCS_BUCKET_NAME", GCS_BUCKET_NAME),
            ("GCS_OBJECT_PREFIX", GCS_OBJECT_PREFIX),
        )
        if not value
    ]
    if missing:
        raise ImproperlyConfigured(
            "GCS_ENABLED=True requires: " + ", ".join(missing)
        )
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.gcloud.GoogleCloudStorage",
            "OPTIONS": {
                "project_id": GCS_PROJECT_ID,
                "bucket_name": GCS_BUCKET_NAME,
                "location": GCS_OBJECT_PREFIX,
            },
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


AUTH_USER_MODEL = "accounts.User"

APPEND_SLASH = False

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]


# JWT 서명 키.
# DEBUG 여부와 무관하게 필수.
# SECRET_KEY 로 대체하면 두 용도가 키를 공유해서 한쪽 유출이 양쪽 유출이 된다.
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise ImproperlyConfigured(
        "JWT_SECRET 환경변수가 필요합니다.\n"
        '  생성: python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_HOURS = 1
REFRESH_TOKEN_HOURS = 12

# KOTH 팀 토큰은 원문을 DB에 보관하지 않고 이 별도 비밀값으로 결정적으로
# 생성한다. 기존 개발 환경은 JWT_SECRET을 fallback으로 사용하되 운영에서는
# 반드시 별도 값을 설정한다.
KOTH_TEAM_TOKEN_SECRET = os.getenv("KOTH_TEAM_TOKEN_SECRET", JWT_SECRET)
# Shared rankings may lag score/team changes by at most this many seconds.
KOTH_LEADERBOARD_CACHE_SECONDS = max(
    0, int(os.getenv("KOTH_LEADERBOARD_CACHE_SECONDS", "1"))
)


REST_FRAMEWORK = {
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%SZ",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.common.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.common.permissions.IsAuthenticated",
    ],
    "EXCEPTION_HANDLER": "apps.common.exceptions.envelope_exception_handler",
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/min",
    },
}
SCHEDULER_API_TOKEN = os.getenv("SCHEDULER_API_TOKEN", "")
SCHEDULER_BASE_URL = os.getenv("SCHEDULER_BASE_URL", "http://127.0.0.1:8001").rstrip(
    "/"
)
_scheduler_url = urlsplit(SCHEDULER_BASE_URL)
try:
    _scheduler_url.port
except ValueError as error:
    raise ImproperlyConfigured(
        "SCHEDULER_BASE_URL 포트가 올바르지 않습니다."
    ) from error

if (
    _scheduler_url.scheme not in {"http", "https"}
    or not _scheduler_url.hostname
    or _scheduler_url.username
    or _scheduler_url.password
    or _scheduler_url.path not in {"", "/"}
    or _scheduler_url.query
    or _scheduler_url.fragment
):
    raise ImproperlyConfigured(
        "SCHEDULER_BASE_URL은 사용자 정보, 경로, query, fragment가 없는 "
        "http 또는 https 주소여야 합니다."
    )

SCHEDULER_TIMEOUT_SECONDS = int(os.getenv("SCHEDULER_TIMEOUT_SECONDS", "5"))

# 새 비밀값은 첫 키로 암호화하고, 이전 키는 복호화에만 사용한다
RUNTIME_SECRET_ENCRYPTION_KEYS = [
    key.strip() for key in os.getenv("RUNTIME_SECRET_ENCRYPTION_KEYS", "").split(",") if key.strip()
]
RUNTIME_SECRET_API_TOKEN = os.getenv("RUNTIME_SECRET_API_TOKEN", "")
INSTANCE_EXTEND_MINUTES = int(os.getenv("INSTANCE_EXTEND_MINUTES", "30"))

# 공급망 publish bundle 자동 수집 (manage.py poll_releases)
RELEASE_POLL_API_BASE = os.getenv("RELEASE_POLL_API_BASE", "https://api.github.com")
RELEASE_POLL_REPO = os.getenv("RELEASE_POLL_REPO", "MSG-CTF/2026_MSG_CTF")
# Actions artifact 다운로드는 공개 저장소여도 토큰이 필요하다 (actions:read)
RELEASE_POLL_GITHUB_TOKEN = os.getenv("RELEASE_POLL_GITHUB_TOKEN", "")
RELEASE_POLL_LIMIT = int(os.getenv("RELEASE_POLL_LIMIT", "100"))
RELEASE_POLL_SOURCE_REF = os.getenv("RELEASE_POLL_SOURCE_REF", "refs/heads/main")

# Optional read-only Git mirror; otherwise use the configured repository's GitHub API.
REPOSITORY_ENV_GIT_DIR = os.getenv("REPOSITORY_ENV_GIT_DIR", "")
REPOSITORY_ENV_REF = os.getenv("REPOSITORY_ENV_REF", "main")
REPOSITORY_ENV_EXCLUDED_SLUGS = {"pwn-msgdriver"}


def _validated_base_url(name, default):
    # 사용자 정보·query·fragment 없는 http(s) 주소만 받는다.
    # 브로커는 프록시 뒤 /api 아래에 있을 수 있어 경로는 허용한다.
    url = os.getenv(name, default).rstrip("/")
    parts = urlsplit(url)
    try:
        parts.port
    except ValueError as error:
        raise ImproperlyConfigured(f"{name} 포트가 올바르지 않습니다.") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ImproperlyConfigured(
            f"{name}은 사용자 정보, query, fragment가 없는 http 또는 https 주소여야 합니다."
        )
    return url


RESOURCE_BROKER_BASE_URL = _validated_base_url(
    "RESOURCE_BROKER_BASE_URL", "http://127.0.0.1:8002"
)
RESOURCE_BROKER_TIMEOUT_SECONDS = int(os.getenv("RESOURCE_BROKER_TIMEOUT_SECONDS", "5"))
INVENTORY_API_TOKEN = os.getenv("INVENTORY_API_TOKEN", "")
