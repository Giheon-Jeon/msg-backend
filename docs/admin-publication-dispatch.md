# Admin Publication Dispatch

This extends the record-only publication request API. It does not import bundles,
activate releases, download images to nodes, or replace running instances.

## Configuration

- Keep `PUBLICATION_DISPATCH_ENABLED=False` until the problem repository caller
  PR #99 is deployed and an approved integration test is completed.
- Recording remains controlled separately by `PUBLICATION_REQUESTS_ENABLED`.
- Set `PUBLICATION_DISPATCH_GITHUB_TOKEN` through the approved secret channel.
  Scope it to `MSG-CTF/2026_MSG_CTF` with Actions: Write and Contents: Read.
  Do not replace the existing read-only `RELEASE_POLL_GITHUB_TOKEN`.
- Apply `instances.0015_publication_dispatch_tracking` before deploying this code.
  It adds tracking fields; it does not delete or rewrite existing records.

## Administrator API

1. `POST /api/v1/admin/publication_requests` with an `Idempotency-Key`,
   `challenge_slug`, lowercase 40-character `requested_sha`, and optional boolean
   `runtime_smoke`. This only records the intent. Legacy records may have no SHA;
   they cannot be dispatched without creating a new explicit request.
2. `POST /api/v1/admin/publication_requests/{request_id}/dispatch` with `{}`.
   The service verifies main's SHA, commits a dispatch marker, then sends the
   fixed workflow request. Recording and dispatch are deliberately separate.
3. `POST /api/v1/admin/publication_requests/{request_id}/sync` with `{}`.
   This performs GET requests only. List/detail GETs remain database-only.

The workflow is `challenge-validation.yml`, repository is `MSG-CTF/2026_MSG_CTF`,
and ref is `main`. Inputs include the persisted request UUID, problem slug,
`expected_source_sha`, empty `source_ref`, `publish_images=true`, and the recorded
smoke option. Neither the destination nor credentials come from browser input.

## States and Retry Rules

- `NOT_DISPATCHED`: no dispatch POST has been attempted. Read/preflight failure
  can be retried on this same record.
- `DISPATCH_UNKNOWN`: the durable marker was committed; acceptance is uncertain.
  Repeated dispatch commands never send another POST, including after a worker
  crash. A zero-result search is not proof of rejection.
- `DISPATCHED`: a run ID was received or recovered. This is not publish success.
- `DISPATCH_REJECTED`: main SHA mismatch or a definitive dispatch HTTP rejection.
  Correct the cause and record a new intent; this UUID is not redispatched.
- `DISPATCH_CONFLICT`: multiple matching runs or invalid run provenance. Do not
  choose the newest result or automatically dispatch again; review is required.

Equivalent intents reuse an active, uncertain, conflicted, or successful request
with the same repository, workflow, source SHA, problem and smoke option.
`reused_request_id`/`dispatch_request_id` identify the canonical tracking record.
Failed completed runs can be followed by a new, explicitly recorded intent.

The original POST receipt is immutable; use list/detail for current state. New
commands use the persisted request UUID as their operation identity and accept no
editable request fields. External calls are outside DB transactions. A crash
after the marker but before POST can also leave UNKNOWN and needs investigation,
not a blind retry.

## Recovery and Verification

Recovery matches the entire title `문제 요청 <slug> / <request_id>` plus official
repository, workflow path, workflow_dispatch event, main branch, source SHA and
creation time. The query window is five minutes before through fifteen minutes
after the saved dispatch timestamp. Searches paginate up to GitHub's 1,000-result
limit. Incomplete searches stay unresolved; they never authorize another POST.
Later/ambiguous runs require manual investigation rather than widening the
request or changing its SHA silently.

`run_sha_verified` refers only to validated run provenance. `sha_verified` remains
false: bundle SHA and artifacts are not inspected by these commands. Workflow
status/conclusion are separate from publish bundle validity, Backend registration,
active version, and node readiness. Run attempt can increase without changing the
stored run number/revision. Older observations cannot replace a newer attempt.

Import a successful artifact through the existing preview/import API separately.
Before connecting automatic import, independently enforce
`requested_sha == workflow_run.head_sha == bundle.source_sha`, explicit artifact
selection on ambiguity, and no overwrite of existing revision digests or files.

Never copy raw workflow logs or tokens into API responses. The implementation
stores only bounded, validated run metadata and static error codes, constructs its
own Actions links, and does not follow redirects or returned API URLs.

References: [DevSecOps contract](https://github.com/MSG-CTF/msgctf-devsecops/pull/28),
[GitHub dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow).
