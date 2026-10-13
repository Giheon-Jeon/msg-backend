# Board/KOTH local load tests

This suite targets only the isolated services in `docker-compose.loadtest.yml`.
It never uses the repository's normal PostgreSQL or Redis containers.

1. Start: `docker compose -f docker-compose.loadtest.yml up -d --build db redis api`
2. Seed: `docker compose -f docker-compose.loadtest.yml exec api python loadtests/prepare_data.py seed`
3. Prepare a write scenario when needed: `... python loadtests/prepare_data.py scenario dice_roll`
4. Run Locust headless using `loadtests/run_suite.ps1`.
5. Run exact same-team races using `loadtests/race_test.py`.

Generated JWTs, internal tokens, and results are ignored by Git.
