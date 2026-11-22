# Deploying Custom MCP

This is the operator walkthrough for running the gateway with Docker. For the
security posture and the checklist of controls, read
[`docs/SECURITY.md`](SECURITY.md) first.

## What you are deploying

- `app` — the Python service (`custom-mcp = custom_mcp.main:run`) built from the
  repository `Dockerfile`. It runs as a **non-root** user (`app`, uid 10001) and
  persists plugin packages and state under `/data` (`DATA_DIR`).
- `redis` — a private `redis:7-alpine` with append-only file (AOF) persistence.
  It is on an internal Compose network with **no published host port**.

Both services are defined in `docker-compose.yml`. The named volumes are `data`
(mounted at `/data` in `app`) and `redis-data` (mounted at `/data` in `redis`,
held by Redis AOF).

> **Read the warnings at the bottom of this page before going to production.**
> They are not optional.

## Prerequisites

- Docker Engine with the Compose v2 plugin (`docker compose`, not
  `docker-compose`).
- A public HTTPS origin you will put a TLS-terminating reverse proxy in front of.
- A strong owner secret (`MCP_SECRET`).

## Required environment

| Variable | Required | Notes |
|----------|----------|-------|
| `MCP_SECRET` | **Yes** | The single owner secret. Must be **at least 16 characters**; the app refuses to start otherwise (`src/custom_mcp/config.py`). Used to authenticate API/MCP requests and to authorize dashboard login. Generate one, for example `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `BASE_URL` | **Yes** | The public origin the gateway is reachable at, as a **bare `http(s)` origin** — no path, no trailing slash, no query/fragment/userinfo (`config.py`). `https://…` enables `Secure` session cookies automatically; `http://` is acceptable only for local use. |

Everything else has a default and is optional: `REDIS_URL` (Compose pins it to
`redis://redis:6379/0`), `DATA_DIR` (`/data`), `HOST` (`0.0.0.0`), `PORT`
(`8000`), `COOKIE_SECURE` (derives from `BASE_URL` scheme), `SESSION_TTL_HOURS`
(default 168), `LOG_LEVEL`, and the ping/tool/worker timeout and size limits — see
`.env.example` and `src/custom_mcp/config.py`.

## Build the image

```bash
docker build -t custom-mcp .
```

The Dockerfile is multi-stage: dependencies are resolved deterministically from
the committed `uv.lock`, and only the virtualenv and `src/` are copied into the
slim runtime stage. No secret is baked into the image.

## Run with Compose

```bash
cp .env.example .env
# edit .env and set MCP_SECRET and BASE_URL
docker compose up -d --build
```

`--build` rebuilds the app image. The app waits for Redis to report `healthy`
(`depends_on: redis: condition: service_healthy`) before starting. Confirm:

```bash
docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
# {"status":"ok"}
```

`GET /healthz` returns `200 {"status":"ok"}` when the app can reach Redis and
`503 {"error":{"code":"unavailable", …}}` when it cannot. The image's
`HEALTHCHECK` probes the same endpoint. To stop without losing data, use
`docker compose down` (keeps both named volumes); `docker compose down -v`
**deletes** the `data` and `redis-data` volumes.

Open the dashboard at `/` in a browser. An unauthenticated browser request is
redirected to `/login`, where you sign in with `MCP_SECRET`.

## HTTPS reverse proxy (nginx)

Terminate TLS at the proxy and forward to the app's published port. MCP uses
streamable HTTP, so the proxy **must not buffer responses** and **must allow long
read timeouts**; otherwise an MCP request can stall or be cut off.

```nginx
server {
    listen 443 ssl;
    server_name mcp.example.com;

    ssl_certificate     /etc/letsencrypt/live/mcp.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mcp.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;

        # HTTP/1.1 to the backend, with upgrade handling for streaming.
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Upgrade $http_upgrade;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # MCP streaming: never buffer, and allow long-lived reads.
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

Set `BASE_URL=https://mcp.example.com` to match this proxy. The app compares the
browser `Origin`/`Referer` against `BASE_URL` exactly for CSRF, so the public
origin must be the one users actually visit.

> **Rate-limit note.** Login rate limiting keys on the *transport peer* IP and
> deliberately ignores `X-Forwarded-For` (it is spoofable). Behind a proxy, all
> logins therefore share the proxy's IP and one rate-limit bucket. This is safe
> but coarse; if you need per-client limits, add them at the proxy.

## Volumes, backup and restore

- `data` (→ `/data`): stored plugin ZIPs (`packages/{slug}.zip`), the extracted
  code regenerated on startup (`plugins/{slug}/`), and scratch. Losing it loses
  your installed plugins.
- `redis-data` (→ `/data` in Redis): AOF holding plugin data **and secrets**,
  dashboard sessions, OAuth state and redacted plugin logs. With AOF on, this
  survives restarts.

Inspect or snapshot a volume with Docker:

```bash
docker volume ls
docker run --rm -v custom-mcp_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/data-volume.tgz -C /data .
```

The application-level backup is the supported portable path: in the dashboard's
**Settings** area, **Download backup** streams a ZIP
(`GET /api/backup`) containing every plugin's package plus `data.json` and
`secrets.json`, and **Restore** (`POST /api/restore`) imports one in `skip` or
`replace` mode. See the README's [Backup and restore](../README.md#backup-and-restore)
section for the exact archive layout.

> **The backup ZIP is plaintext.** It contains every plugin's stored credentials
> and the reserved `core.*` secrets in readable JSON. Store it encrypted, and
> never commit, email or share it.

## Upgrade procedure

```bash
git pull                       # or check out the new revision
docker compose up -d --build   # rebuild the app image and recreate the container
```

There is no separate migration step. On startup the manager validates settings
and Redis, cleans scratch, re-extracts each stored package, loads/mounts plugins
and reconnects stored auth modes, then starts the health scheduler. A
replacement/reload is transactional, so a package that fails to load restores the
previous code instead of destroying a working plugin.

If a storage commit's outcome could not be reconciled, startup **fails fast**
with an "unresolved plugin upgrade" error and preserves both old and new code
under `DATA_DIR/tmp/pending-*` for operator reconciliation; it will not load a
possibly-mixed state. Do not delete those directories unless you have decided
which generation to keep.

## Logs

- **Service logs** are structured JSON on stdout:
  `docker compose logs -f app` (and `redis`). Set `LOG_LEVEL` in `.env` to
  control verbosity.
- **Per-plugin logs** are redacted, capped at 200 entries, and shown in the
  dashboard plugin detail view; they live in Redis at `core:logs:{slug}`.
  Credentials are redacted before they are written (see
  [`docs/SECURITY.md`](SECURITY.md) control 8).

## Health endpoint

`GET /healthz` is public and unauthenticated. It pings Redis and returns
`{"status":"ok"}` with `Cache-Control: no-store` on success, or `503` when Redis
is unavailable. Use it for load balancers, uptime checks and the container
`HEALTHCHECK`.

## Required warnings

1. **Redis without persistence loses credentials on restart.** Every
   `plugin:{slug}:secret:*` credential and every dashboard session lives in
   Redis. The shipped `docker-compose.yml` enables AOF (`--appendonly yes
   --appendfsync everysec`) on the `redis-data` named volume so they survive. If
   you run Redis without persistence, a restart silently drops all stored
   credentials and sessions.
2. **Trusted plugin code can read the environment.** Plugins are trusted
   in-process Python. A plugin can read `MCP_SECRET`, `REDIS_URL` and every other
   environment variable, and can access the network and filesystem as the
   runtime user. Only install plugins you trust; there is no sandbox.
3. **Backups are PLAINTEXT.** `/api/backup` produces an unencrypted ZIP
   containing credentials. Treat it like a password and store it encrypted.
4. **NEVER expose Redis publicly.** Redis has no authentication configured by
   this app and holds sessions, credentials and OAuth state. Keep it on the
   private Compose network; the shipped stack publishes no Redis port. Never
   `ports:`-publish `6379`.
5. **HTTPS is required outside local use.** `MCP_SECRET` and bearer tokens cross
   the wire on every API/MCP call. Terminate TLS at a reverse proxy and set
   `BASE_URL` to the `https://` origin. A bare `http://` `BASE_URL` is for
   localhost only.

## Local (non-Docker) quick start

For development without containers:

```bash
uv sync
export MCP_SECRET="a-strong-secret-value"
export BASE_URL="http://127.0.0.1:8000"
redis-server &          # must be reachable at REDIS_URL
uv run custom-mcp
```

The dashboard is then at `http://127.0.0.1:8000/` and health at `/healthz`.
