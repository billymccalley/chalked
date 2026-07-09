# Chalked Backend

Small dependency-free backend foundation for Chalked.

## What it supports now

- User registration and login with DB-backed session cookies
- Email verification, password reset, password changes, and session management
- League creation, discovery, joining, ownership, and settings
- League-specific active slates and generated matchups
- League-specific picks, bankroll limits, multipliers, and leaderboards
- MLB schedule/live-feed stat sync for slate settlement
- Protected scheduled settlement endpoint for production cron jobs
- Persistent playoff brackets and image uploads
- SQLite persistence
- League activity feed
- A player-data provider boundary with static fallback when MLB StatsAPI is unavailable

## Run locally

```powershell
python -m backend.chalked_backend.server
```

The API starts on `http://127.0.0.1:8080`.

Open the app at:

```text
http://127.0.0.1:8080/
```

Use the seeded demo account:

```text
handle: demo
password: demo12345
```

## Useful endpoints

- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/sessions`
- `POST /api/auth/email/verify/request`
- `POST /api/auth/email/verify/confirm`
- `POST /api/auth/password-reset/request`
- `POST /api/auth/password-reset/confirm`
- `GET /api/me`
- `GET /api/leagues`
- `POST /api/leagues`
- `POST /api/leagues/{league_id}/join`
- `PATCH /api/leagues/{league_id}/settings`
- `GET /api/leagues/{league_id}/slate`
- `POST /api/leagues/{league_id}/picks`
- `DELETE /api/leagues/{league_id}/picks/{pick_id}`
- `GET /api/leagues/{league_id}/leaderboard`
- `GET /api/leagues/{league_id}/playoffs`
- `GET /api/leagues/{league_id}/activity`
- `POST /api/uploads`
- `POST /api/system/settle` protected by `CHALKED_CRON_SECRET`

Slate settlement runs from MLB live feed data when active/final game box scores are available.

Production notes live in `backend/PRODUCTION.md`. The Postgres migration plan lives in `backend/POSTGRES_PLAN.md`.
