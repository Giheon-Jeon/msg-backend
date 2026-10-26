# Board / KOTH QA revalidation — 2026-10-27

Baseline backend: `ea1f02ce6c1042c690f454aca5b65a4e183a540f`.
Baseline frontend: `516fbc9487fc36a32ccf9b95c98a41e5af244b97`.
Backend changes are prepared for a backend-only PR; no production deployment has
been performed. At the user's request, frontend commit `80b3902` was reverted by
`7197d9c`; its tracked tree exactly matches baseline `516fbc9` again. Frontend
findings below are handoff requests, not fixes included in this backend PR.

## Implemented

- Public board definitions and chance catalog, as explicitly specified in the
  canonical API pages. Team board state remains authenticated.
- Apply the existing user/IP read throttle to those definitions and KOTH club
  list/detail. Real Redis at `5/min` returns five HTTP 200s then two 429s with
  `Retry-After` on each of the four endpoints.
- Force-move ends the current challenge reference while preserving earned
  access history, under the same board-state lock as problem opening.
- Refuse discarding a card that owns a pending roll choice; confirmation remains
  possible. Completed-board reads clear an existing future recharge deadline.
- Reject banned teams from KOTH progress and team-token reads using current DB
  state, including existing JWTs.
- KOTH ranking includes recorded zeroes without assigning them a competitive
  rank; `collection_status`, `is_stale`, period and collection timestamps describe
  collection independently of the score. Internal error details are not exposed.
- Team-token key replacement updates `issued_at` as well as the stored hash.

## Frontend handoff (changes reverted)

- TC-439/443: reloads must discard stale responses; starting a mutation must
  invalidate earlier loads. A delayed position-10 response overwrote position 20.
- TC-469/474: preserve `team_card_id` in adapters, keys, selection, use and
  discard. Reconstruct the discard prompt after reloading two held cards.
- TC-506: calculate elapsed time with a monotonic clock after receiving server
  time. A wall-clock jump of one hour made a ten-minute countdown become zero.
- TC-568: display the backend's new collection-state fields independently of
  `earned_score=0`, preserving the distinction between pending, failed and zero.
- Use `active_challenge.solve_deadline_at` rather than a fixed deadline derived
  from `opened_at`; the reward duration is configurable by the backend.

## Verification

- PostgreSQL regression run: 185 tests passed. After public-access and key
  replacement additions, 14 focused/access checks passed (overlap exists).
- Historical frontend prototype validation (now reverted): 204 tests and Vite
  build passed. Those results do not establish that the restored frontend is
  fixed; TC-439/443/469/474/506 remain for the frontend team, as does TC-568 UI.
- `tools/qa_remaining_replay.py`: real Gunicorn HTTP, real PostgreSQL, real Redis;
  66 recorded HTTP requests, 26 assertions passed. The DB name is asserted before
  fixture writes. The script retains raw response/DB evidence with tokens
  redacted in `/tmp/qa_remaining_result.json`.
- Local evidence copy: `C:/msg_BE/output/qa-remaining-fixed-20261027.json`.
- Backend replay endpoint: `http://127.0.0.1:8016`. Dedicated DB:
  `qa_remaining_20261027`. The temporary frontend preview has been stopped.
- Historical browser prototype check (before revert): logged into the real board, selected the second of two identical
  extra-roll cards, and discarded only that UUID. Using the remaining card set
  only its `used_at` and increased dice from 1 to 2. Reload restores the prompt.
- Race and clock cases use executable React tests; no claim is made that OS
  time was changed or network response order was altered through browser UI.

## Corrections to the earlier QA interpretation

Earlier evidence recorded behavior correctly, but some failure judgments imposed
rules absent from the product contract. They must not be counted as code fixes.

- Card and airport APIs have no mileage price or debit rule. Supplying a client
  `cost` field does not create a charge. Fee/insufficient-balance cases are not
  applicable until a paid product rule is introduced.
- Roulette rewards are only 50/100/150/200 mileage. There is no dice reward.
  The completion contract retains mileage from the final move; completion must
  not erase that reward or require an unspecified dice reward flow.
- Existing tests explicitly allow another roll from a roulette cell. Optional
  special-cell effects are distinct from `PendingDiceRoll` confirmation, which
  still blocks another roll. Do not add a blanket special-cell lock.
- KOTH uses one identity token per team, shared across problems; each problem
  authenticates independently using its own internal credential. The token API
  takes no problem ID and declares no TTL. Per-problem token binding and a
  400-day expiry rule cannot be inferred. Key replacement and bans are tested.

Canonical sources inspected on 2026-10-27:

- Board definitions: https://app.notion.com/p/4440f19c72be831982e481cdf32b78dc
- Catalog: https://app.notion.com/p/a740f19c72be831d86ac81e7bfe9dbdf
- Airport: https://app.notion.com/p/c8a0f19c72be83cda10101cf38e1fd07
- Card use: https://app.notion.com/p/da30f19c72be82fbb666811b4348f54d
- Roulette: https://app.notion.com/p/ee90f19c72be822a91d3015e71d33fe8
- Team token: https://app.notion.com/p/3ae0f19c72be81819c97fbfc9273fbf5
- Internal verification: https://app.notion.com/p/3ae0f19c72be8123aad3dca5a0f2ed35
- Completion: `docs/board-api-spec-update.md`.

## Outstanding: TC-589 deployment boundary

`deploy/nginx-internal-boundary.conf.template` separates the public gateway
(`/internal/` returns 404) from a private listener restricted to an explicit
problem-server CIDR. It is a prepared configuration, not an applied deployment.

Before use, supply the actual upstream, public listener, private listener and
allowed caller CIDR. Render only those four variables with `envsubst`, preserving
Nginx `$host` and `$scheme`; run `nginx -t`. Bind Gunicorn to a private/loopback
address and restrict direct access at the firewall so it cannot bypass Nginx.
Keep per-problem `X-Internal-Token` checks enabled.

Acceptance requires external-host requests to be denied and authorized private
problem-server requests to succeed. Localhost access to a QA Gunicorn port alone
does not establish an externally exploitable production routing issue. Actual
production gateway/firewall state was not accessed; TC-589 remains unverified
for deployment and must not be reported as resolved.
