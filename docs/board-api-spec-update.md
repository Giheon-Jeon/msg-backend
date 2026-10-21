# Board API alignment (2026-09-14)

Source: [Notion Board API](https://app.notion.com/p/3ab0f19c72be80ab9e57e3776b67323f).

- The 36-cell board uses START, CHALLENGE, CHANCE, AIRPORT, and ROULETTE.
- Cells 16 and 25 are roulette cells. Each awards 50/100/150/200 with equal probability, once per team per cell.
- The catalog contains five cards: reroll, roll twice and choose, move offset, free travel, and extra roll.
- Quarantine state, escape endpoints, and the two retired card effects have been removed.

## Consecutive dice rolls

Teams may spend stored rolls without waiting for the challenge reward window or dice recharge. A challenge must still be selected on arrival before the next roll. The `timer_running` field reports the current challenge reward window independently of `can_roll`; it no longer produces a `TIMER_RUNNING` block. Empty dice, an unconfirmed roll, and board completion still block rolling. Further rolls preserve the existing recharge deadline, and opened challenges remain available after moving.

## Configured dice capacity and reward window

`GET /api/v1/board/me` and `GET /api/v1/board/dice/status` both return `max_dice_rolls`, the current `board.dice_rolls_per_reset` setting (default 3). New teams start with that many dice. Existing balances are preserved when the setting changes; raising capacity starts recharge when necessary and does not restart an existing recharge deadline.

The client displays `dice_rolls_left / max_dice_rolls` and uses `next_dice_reset_at` for recharge. For the challenge reward countdown, use `active_challenge.solve_deadline_at` from `/board/me` or `solve_deadline_at` from `/board/cell/open`. Do not derive a fixed 15-minute deadline from `opened_at`: changing `board.solve_deadline_minutes` also affects already opened challenges.

## START and board completion (#31)

START (cell 1) can be landed on exactly once per team and is consumed on that first finalized landing. The initial spawn on START does not consume it. Later landings skip START in the movement direction, and airport/free-travel destinations cannot select it after consumption. START remains in `consumed_cell_indexes` and `cell_states`, but completion requires only the 35 other cells (2–36). Consumed cells remain in `movement_path` when traversed. Crossing START grants 100 mileage per finalized move, even after START is consumed; the first exact landing also grants 100 mileage and one roll, subject to the three-roll cap. Passing alone grants no roll. Retrying the same request does not repeat rewards. Once the last non-START cell is consumed, `board_completed` is true, `can_roll` is false with `BOARD_COMPLETED`, and recharge and automatic dice rewards stop. The last move still costs one roll and any mileage reward is retained.

## Existing databases

### START completion migration

Migration `board.0005_exclude_start_from_completion` clears recharge deadlines for teams that consumed cells 2–36. All consumption rows, including START, positions, dice balances, solves and reward history are preserved. Run `python manage.py migrate` when deploying; do not reseed an existing game. Reversing this data migration does not restart completed teams' timers.

### Board layout migration

Apply `python manage.py migrate` with the new release. Migration `board.0004_align_board_api_spec` updates cell 16 in place and translates pending landings on it. It also updates special-cell labels and the move-offset description.

Team positions, dice balances and recharge times, consumed cells, challenge access/solve records, and mileage are preserved. Retired card definitions and draw history remain for auditing; previously held retired cards are marked discarded and excluded from gameplay responses. No new card or dice reward is granted during conversion. Schema rollback does not reactivate these cards or restore deleted escape codes.

Do not run `seed_board` to update an existing game: that command resets board progress. Use it only when deliberately initializing a fresh demo board. Restart backend processes after applying the migration so their model definitions match the database.

## Verification

Run `python manage.py test apps --noinput` against PostgreSQL. Tests cover the fixed layout, both roulette cells and duplicate rewards, the five-card catalog and flows, removal of escape routes, and migration of an existing game. The migration test uses historical models, checks that team progress and mileage remain intact, and continues through the real board API after conversion.

Migration tests register cleanup before downgrading and restore all saved latest migration leaves, including when setup or assertions fail. Runtime tests exercise concurrent requests, cache failures, transaction rollback, and shared-card row-lock contention. See [PR #72 validation](board-pr72-validation.md) for the scenarios and reviewer criteria.

Card use/discard locks the team's inventory rows without locking shared card definitions. A fresh team's dice-confirm request without a pending roll returns `409 NO_PENDING_ROLL`. Endpoints that take JSON objects reject arrays, scalars, and null with `400 INVALID_REQUEST`; bodyless actions reject supplied non-object JSON with `400 REQUEST_BODY_NOT_ALLOWED`, including falsy values. An absent body and the existing empty-object form remain accepted by bodyless actions.
