# Backend protected image inventory

Run against the intended deployed Backend database, using its confirmed deployment
commit SHA (not a workstation branch SHA). This command does not contact GHCR,
Scheduler, Runtime, or nodes. It never modifies database records or deletes images.

```powershell
python manage.py export_protected_images --environment development --deployment-sha <confirmed-40-character-sha> --output .tmp/protected-images.json
```

Create the output directory first. Use a new output filename for each snapshot;
existing files are not overwritten. Without `--output`, JSON is written to stdout.
The environment label and SHA are operator-supplied claims, not automatic proof
of the deployed environment. Database credentials and connection strings are not
exported. PostgreSQL scans use one repeatable-read, read-only transaction; other
database engines are explicitly marked as having unverified snapshot consistency.

## Scope

- All registered releases are temporarily protected, including inactive releases.
  Inactive does not mean approved for future deployment or deletion.
- Images are deduplicated by the full pinned GHCR reference. Every release and
  container reference is retained, including private helper containers.
- Active runtime configurations, all participant instance history, and all admin
  deployment tests are attached as additional references. History is deliberately
  not filtered by status. Non-CLEANED records have unverified cleanup status;
  even CLEANED is only a Backend record, not independently verified Runtime state.
- Missing releases, missing publication identity, releases without containers,
  and invalid/unpinned image references appear under `unresolved`. Raw malformed
  image values are omitted because they might contain credentials.
- `database_scan_complete` describes traversal of the listed Backend tables only.
  Empty results do not establish global non-use. Failures abort the export.
- Scheduler queues/reset/retry, Runtime policy and actual running images, node
  downloads, GHCR existence/access and approved rollback plans remain unverified.
- KOTH, Windows and platform images remain outside deletion review. Their global
  inventory is not claimed here; any registered release is conservatively kept.

## Rollback and handoff

This JSON is an image inventory, not a rollback backup. Preserve the original
release records, runtime settings, immutable secret versions, participant bundle
bindings and actual stored ZIP objects separately. Their availability/integrity
requires separate verification. No environment values, secret values/bindings,
notes, artifact payloads or participant file contents are exported.

Send the inventory and all unresolved entries to DevSecOps. Merge it with the
Scheduler/Runtime owner reports before evaluating candidates. `deletion_authorized`
is always false. Refresh snapshots and obtain a separate review/approval before
any deletion; inventories become stale as deployment work continues.
