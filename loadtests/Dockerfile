FROM python:3.12-slim

WORKDIR /loadtests
RUN python -m pip install --no-cache-dir "locust>=2.40,<3" "requests>=2.32,<3"
COPY . /loadtests
