# Gateway v1 Endpoint Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve Gateway connection metadata from Scheduler in participant and administrator endpoint responses.

**Architecture:** Extend the shared Scheduler endpoint normalizer. Existing JSON fields and serializers retain the normalized endpoint without model, migration, URL, or frontend changes.

**Tech Stack:** Python 3.12, Django 5.2, PostgreSQL 16, Django test runner.

**Spec:** [Gateway v1 design](https://github.com/MSG-CTF/secure-provisioner/pull/74), [Runtime implementation](https://github.com/MSG-CTF/secure-provisioner/issues/73).

## Global Constraints

- Gateway v1 retains companion containers, one public endpoint, and NodePort upstream.
- Existing container_name, port (internal container port), protocol (HTTP or TCP), and service_url retain their meaning.
- Preserve optional hostname, connection_port, tls, server_name exactly when present, including null and false. Do not synthesize defaults or infer them from URLs.
- Non-null hostname/server_name are nonempty strings; connection_port is an integer 1–65535 excluding booleans; tls is a boolean.
- TCP public service_url is tls://hostname:443; HTTP is https://hostname. Backend forwards these values without contacting Gateway.
- No real token, service, deployment setting, frontend, or database migration changes.

## Review Focus

- Optional keys missing in legacy responses stay missing.
- Explicit null and tls=false survive persistence and response serialization.
- Boolean ports, string TLS flags, empty names, and out-of-range connection ports are rejected before database mutation.
- Internal port remains distinct from public connection_port.
- Non-running instances and administrator tests continue hiding endpoints.

### Task 1: Preserve and validate endpoint metadata

**Files:** Modify `apps/instances/services.py`; add `apps/instances/tests_gateway_endpoints.py`; document `docs/gateway-v1-endpoint-metadata.md`.

**Interfaces:** `normalize_scheduler_endpoints(raw_endpoints)` consumes Scheduler endpoint dictionaries and returns validated copies for instance and deployment-test persistence and public serializers.

- [x] Add tests that exercise Gateway HTTP/TCP, missing/null/false values, invalid optional values, participant create/update storage and serialization, and administrator test storage and serialization.
- [x] Run the new tests before changing production code; require failures showing lost fields or accepted invalid values.
- [x] Add only the four agreed optional fields to the shared normalizer with strict non-null type/range checks.
- [x] Run the new tests, all instance tests, and `python manage.py test apps --noinput`; record any existing failures explicitly. All 1,114 app tests passed on PostgreSQL 16 (605.318s).
- [x] Run Django checks, migration drift check, and `git diff --check`; review the complete diff.
- [ ] Commit, push the isolated feature branch, and create a draft Backend PR linked to its tracking issue and Runtime design/implementation. Attach the PR to the current chat.
