# Phase 9 plan — security audit, deployment, documentation and CI

Goal: make the service deployable and auditable — a non-root container image, a
Compose stack with a persistent Redis and no public Redis port, CI for the full
test/lint/type/real-Redis gates, a security checklist, operator documentation,
and a fresh-deployment smoke test.

## Acceptance gate (TEST_MATRIX row 9)
Full cumulative tests; Ruff and a meaningful public-function type check; isolated
real-Redis integration job with AOF; security checklist audit; non-root Docker app
and persistent volumes; Compose startup health dependencies; fresh deployment
smoke; README setup/authoring/backup/HTTPS/proxy/no-Redis-exposure guidance.

## Contract (ARCHITECTURE.md "Release", lines 176–188)
- Three examples (`hello` none, `apikey_demo` api-key, `oauth_demo` OAuth) each
  with manifest, `plugin.py` and a short author README.
- Non-root Docker; app persistent `DATA_DIR`; Redis AOF named volume and private
  network; single instance behind an HTTPS reverse proxy with MCP buffering
  disabled.
- README must warn explicitly: Redis without persistence loses credentials on
  restart; trusted plugin code can read the environment; backups are plaintext;
  never expose Redis publicly; HTTPS is required outside local use.
- CI runs the full pytest/fake suite and the isolated real-Redis tests, Ruff and
  type checks.
- Startup validates settings/Redis, cleans scratch, reextracts, loads/mounts/
  reconnects, starts the scheduler. Shutdown stops the scheduler, shuts plugins,
  closes mounted lifespans, then Redis (already implemented; the deployment must
  preserve it).

## Environment facts
- Docker **29.6.1** and Compose **v5.3.0** are available locally.
- `Settings`: `MCP_SECRET` (≥16 chars, required), `BASE_URL` (bare http(s) origin,
  required), `REDIS_URL`, `DATA_DIR`, `HOST` (default `0.0.0.0`), `PORT` (8000),
  `COOKIE_SECURE` (defaults to `BASE_URL` scheme), plus the timeouts/limits/
  `SESSION_TTL_HOURS`/`LOG_LEVEL`.
- `tests/real_redis/` starts its **own** port-disabled Unix-socket Redis with
  `appendonly yes`; CI must install a `redis-server` binary (not merely a service)
  and must not touch any external Redis.
- Entry point: `custom-mcp = custom_mcp.main:run`; health at `GET /healthz`.
- Git is on an unborn `master` with everything untracked; do not commit/push.

## Slices (non-overlapping ownership)
### 9a Deployment artifacts (one agent) — owns these files only
- `Dockerfile`: multi-stage (builder + runtime) using a pinned `python:3.11-slim`
  (or `uv`-based) base; install from `uv.lock` deterministically; create and run
  as a **non-root** user; `WORKDIR /app`; copy `src/`; `DATA_DIR=/data` as a
  volume; `EXPOSE 8000`; a `HEALTHCHECK` hitting `/healthz`; `CMD
  ["custom-mcp"]` (or `uvicorn custom_mcp.main:app`-equivalent used by `run()`).
  No secrets baked in.
- `docker-compose.yml`: `app` (build ., env from `.env`/`environment`,
  `depends_on: redis: condition: service_healthy`, port `8000`, named `data`
  volume at `/data`, private network) and `redis` (`redis:7-alpine`,
  `appendonly yes` + `appendfsync everysec`, named `redis-data` volume, **no
  published port**, a `redis-cli ping` healthcheck, private network only).
- `.dockerignore`, `.env.example` (no real secret), and an optional
  `docker/redis.conf` if needed for AOF.
- `tests/deployment/test_artifacts.py`: static assertions that the Dockerfile
  switches to a non-root user (no `USER root` at the end), the Compose file has a
  Redis AOF volume, does **not** publish a Redis port, defines a redis
  healthcheck + `depends_on` condition, mounts a persistent `DATA_DIR`, and that
  `.env.example` contains no real secret.

### 9b CI (one agent) — owns these files only
- `.github/workflows/ci.yml`:
  - `lint` job: `uv sync --frozen`, `ruff check .`, `mypy`.
  - `test` job: `pytest -q` (fake Redis).
  - `real-redis` job: install `redis-server`, run `pytest -q tests/real_redis`
    (the harness owns its Unix-socket instance with AOF; assert it never needs an
    external service).
  - `docker` job: `docker build` (and optionally `docker compose config`) to prove
    the image builds.
  - Pin the Python version and use `uv.lock`; fail on lock drift (`uv lock
    --check`). Do not add secrets.

### 9c Security audit + docs (one agent) — owns these files only
- `docs/SECURITY.md`: a checklist mapping each required control to where it is
  implemented/tested (public-route allowlist, bearer-only MCP, cookie CSRF
  origin/referer, constant-time secret comparison, rate limiting, hardened
  session cookie, CSP/nosniff/frame-ancestors, no-store, secret redaction in
  logs/descriptions/errors, ZIP validation, scoped storage, sealed retired
  generations, single-use slug-bound OAuth state, PKCE, plugin trust boundary).
  Mark any accepted MVP limitation honestly.
- `README.md`: add/expand a Deployment section (Compose up, required env, HTTPS
  reverse proxy with MCP buffering disabled, backup/restore, upgrade) and include
  the explicit warnings listed in the contract above. Keep it accurate; do not
  claim checks that were not run.
- `docs/DEPLOYMENT.md`: operator walkthrough (build, compose, proxy example with
  `proxy_buffering off`/streaming, volume/backup notes, logs, health).

### 9d Fresh-deployment smoke (manager-run, after 9a)
- Build the image and run the Compose stack with a throwaway `MCP_SECRET` and
  `BASE_URL=http://127.0.0.1:8000`; wait for Redis `healthy` and the app; verify
  `/healthz`, `/login`, an authenticated `/api/plugins`, import `hello`, and an
  MCP `initialize`/`tools/list` call; confirm Redis is **not** reachable from the
  host and the container runs as non-root; then tear down (`down -v`). Record the
  exact commands and output. If Docker is unavailable at run time, record it as
  an unavailable check rather than a pass.

## Definition of done
- Image builds and runs non-root; Compose starts with a healthy Redis dependency
  and a persistent Redis AOF volume with no published Redis port; artifacts are
  covered by static tests.
- `docs/SECURITY.md` checklist is complete and accurate; README/DEPLOYMENT warn
  about Redis persistence, plaintext backups, trusted code and HTTPS/no public
  Redis.
- CI workflow runs the full suite, Ruff, mypy, the isolated real-Redis job and a
  Docker build.
- Fresh-deployment smoke passes (or is explicitly recorded unavailable).
- Full cumulative suite, Ruff, mypy, `uv lock --check`, `uv build` and the 15
  real-Redis tests all pass.
- Independent Phase 9 gate critic scores strictly above 8.5 (MVP-only policy),
  followed by the final independent diff review.