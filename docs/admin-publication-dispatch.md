# Admin Publication Dispatch

This extends the record-only publication request API with explicit dispatch/sync
and optional request-bound preview/import. It does not automatically import
bundles, activate releases, download images to nodes, or replace running instances.

## Configuration

- Keep `PUBLICATION_DISPATCH_ENABLED=False` until the problem repository caller
  PR #99 is deployed and an approved integration test is completed.
- Recording remains controlled separately by `PUBLICATION_REQUESTS_ENABLED`.
- Set `PUBLICATION_DISPATCH_GITHUB_TOKEN` through the approved secret channel.
  Scope it to `MSG-CTF/2026_MSG_CTF` with Actions: Write and Contents: Read.
  Do not replace the existing read-only `RELEASE_POLL_GITHUB_TOKEN`.
- Apply `instances.0015_publication_dispatch_tracking` and
  `instances.0016_publicationrequest_collection` before deploying this code.
  They add tracking fields; they do not delete or rewrite existing records.

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

`run_sha_verified` refers only to validated run provenance. Dispatch/sync alone
never set `sha_verified` to true: bundle SHA is not inspected by those commands. Workflow
status/conclusion are separate from publish bundle validity, Backend registration,
active version, and node readiness. Run attempt can increase without changing the
stored run number/revision. Older observations cannot replace a newer attempt.

## Request-Bound Collection

Use the existing explicit selection flow, optionally bound to a publication request:

```http
POST /api/v1/admin/published_challenges/preview
{
  "artifact_id": 123456,
  "publication_request_id": "<request UUID>"
}
```

The UUID must identify a dispatched request with a known run ID, or an alias of
that request. Sync an uncertain request first; collection does not guess a run or
dispatch anything. Both the problem and run ID must match. The official repository,
workflow, main, full request title, successful run and source SHA are revalidated.
Each included bundle must satisfy
`requested_sha == workflow_run.head_sha == bundle.source_sha`; bundle revision
must equal the run number. The artifact name's attempt need not equal the run's
current attempt. Multiple candidates for a bundle kind are rejected, not sorted
by newest. Missing/expired artifacts and changed source metadata are rejected.

The preview token signs the request binding and the verified run attempt as well
as the selected artifact/content fingerprint. Submit that token through the same
`POST /api/v1/admin/published_challenges/import` API with `difficulty` and an
`Idempotency-Key`; do not add another request UUID to the import body. Import
re-reads GitHub and checks the binding again under the DB lock before saving.
Changing run attempts after preview requires a fresh preview. Concurrent newer
observations also require retrying from preview. Existing revision images/files
are never overwritten with conflicting content.

Only a successful import commits `collection` on the canonical publication
request, in the same DB transaction as the release/files. It records the selected
artifact, all included artifact IDs by bundle kind, run ID/attempt, source SHA,
revision, release ID (null for static publications), and import time. Preview
alone never writes this proof. A later successful import may refresh this proof;
each import response remains in the existing idempotency receipt.

Request list/detail expose `collection`. Their `sha_verified` is true only when
the saved proof matches the current dispatched run, SHA and attempt. If a later
run attempt is synced, the old proof remains visible but `sha_verified` becomes
false until that attempt is imported. This is SHA/provenance proof for the
included artifacts, not release activation, complete node readiness, or a claim
that a missing participant-files bundle was collected. Import still reports
`files_status=pending` when that bundle is absent.

Omitting `publication_request_id` preserves legacy successful-publication
collection: no historical request UUID or dispatch credential is required, and
bundle-to-run SHA validation is unchanged. Automatic import and real caller-to-
Backend integration testing remain separate work.

Never copy raw workflow logs or tokens into API responses. The implementation
stores only bounded, validated run metadata and static error codes, constructs its
own Actions links, and does not follow redirects or returned API URLs.

References: [DevSecOps contract](https://github.com/MSG-CTF/msgctf-devsecops/pull/28),
[GitHub dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[workflow runs](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow).
