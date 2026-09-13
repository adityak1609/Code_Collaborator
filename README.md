# Concord

Concord is a collaborative code editor built with React, Monaco, Yjs, FastAPI,
pycrdt, PostgreSQL, and Redis. The current implementation focuses on Milestone 1:
authenticated sessions, role-based access, real-time editing, awareness/presence,
reconnection, Redis recovery checkpoints, explicit PostgreSQL saves, and live
authorization revocation for connected clients.

## Current status

- Milestone 1: functional vertical slice; automated protocol and business-rule
  tests plus a live three-client smoke test are available.
- Milestone 2: execution schema/state machine only. The queue, worker, sandbox,
  streaming output, and terminal UI are not implemented yet.
- Milestone 3: snapshot schema only. Snapshot APIs, broader integration tests,
  CI, benchmarks, and horizontal-scaling experiments remain.

Collaboration deliberately runs in one backend process. Its in-memory Y.Doc is
authoritative while users are connected, periodically checkpointed to Redis for
crash recovery, and saved to PostgreSQL when an editor explicitly saves. Multiple
Uvicorn workers are not supported in V1.

## Run locally

Requirements: Docker Desktop and Docker Compose.

```powershell
docker compose up -d --build
docker compose ps --all
```

Open <http://localhost:5173>. The API health endpoint is
<http://localhost:8000/health>.

The migration service runs `alembic upgrade head` before the backend starts.
PostgreSQL and Redis use host ports `5433` and `6380` by default so they can
coexist with common local installations. Override them when needed:

```powershell
$env:POSTGRES_PORT = "15432"
$env:REDIS_PORT = "16379"
$env:JWT_SECRET = "replace-with-a-long-random-development-secret"
docker compose up -d
```

For source-mounted hot reload:

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## Verify

Backend:

```powershell
cd backend
.\venv\Scripts\python.exe -m pytest -q
.\venv\Scripts\ruff.exe check app tests
.\venv\Scripts\ruff.exe format --check app tests
```

Frontend:

```powershell
cd frontend
npm.cmd run build
npm.cmd run lint
```

With the Compose stack running, exercise auth, RBAC, three simultaneous Yjs
clients, presence, viewer write/save rejection, reconnect, explicit save, and
Redis recovery. It also verifies shared save metadata plus connected-client
demotion, removal, and session-close enforcement:

```powershell
cd frontend
npm.cmd run smoke:collaboration
```

The smoke test creates uniquely named users and closes its session at the end.

## Configuration

Frontend build variables are documented in `frontend/.env.example`:

- `VITE_API_URL`
- `VITE_WS_URL` (the default includes the `/ws` prefix)

Backend variables are documented in `backend/.env.example`. Never commit a real
`.env`; repository and Docker ignore files exclude secrets and generated files.
Recovery checkpoints expire after seven days by default; configure
`CRDT_CHECKPOINT_TTL_SECONDS` to change that retention window.

## Next milestone

Build Milestone 2 as a tested vertical slice: execution API and transition
service, Redis queue, isolated Docker worker for Python/JavaScript/C++, live
output events, cancellation/timeout behavior, history endpoints, and the
frontend output panel.
