FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

RUN apk upgrade --no-cache

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check \
    && python -m pip uninstall --yes pip

RUN addgroup --system app \
    && adduser --system --ingroup app --home /app app

COPY --chown=app:app . .

ENV HOME=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_CONCURRENCY=2 \
    GUNICORN_WORKER_CLASS=gthread \
    GUNICORN_THREADS=4 \
    GUNICORN_KEEPALIVE=10 \
    GUNICORN_TIMEOUT=30

USER app

EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-2} --worker-class ${GUNICORN_WORKER_CLASS:-gthread} --threads ${GUNICORN_THREADS:-4} --keep-alive ${GUNICORN_KEEPALIVE:-10} --timeout ${GUNICORN_TIMEOUT:-30} --access-logfile - --error-logfile -"]
