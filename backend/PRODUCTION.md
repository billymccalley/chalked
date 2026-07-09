# Chalked Production Setup

This is the private-beta path for turning the local app into a real hosted site.

## Recommended beta stack

- App host: Render Web Service, Railway, Fly.io, or a small VPS/container host
- First smoke test: free Render web service with temporary SQLite
- Database for private beta: SQLite on a persistent disk
- Database for public launch: managed Postgres
- DNS: Cloudflare DNS
- Email: Postmark, SendGrid, Mailgun, or another SMTP provider
- Scheduled jobs: Render Cron Job, GitHub Actions schedule, or host-native cron

## Render-style deploy

The repo includes a starter `render.yaml` for a free smoke test with:

- `chalked`: the web service
- `plan: free`
- `CHALKED_PUBLIC_URL=https://chalked.onrender.com`
- temporary SQLite at `/tmp/chalked.sqlite3`

If Render says the `chalked` service URL is unavailable or assigns a different URL, update `CHALKED_PUBLIC_URL` and `CHALKED_ALLOWED_ORIGINS` to the actual `.onrender.com` URL shown in the Render dashboard.

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
python -m backend.chalked_backend.server
```

The server reads `PORT` automatically, so hosts that inject a dynamic port work without code changes. Locally it still defaults to `127.0.0.1:8080`.

After the smoke test works, upgrade to the paid private-beta setup:

- set the web service plan to `starter`
- add a persistent disk mounted at `/data`
- set `CHALKED_DB=/data/chalked.sqlite3`
- add a cron job that runs `python -m backend.chalked_backend.jobs settle`
- set `CHALKED_PUBLIC_URL` and `CHALKED_CRON_SECRET` on the cron job

## Required environment

```text
CHALKED_ENV=production
CHALKED_HOST=0.0.0.0
CHALKED_PUBLIC_URL=https://chalked.gg
CHALKED_ALLOWED_ORIGINS=https://chalked.gg,https://www.chalked.gg
CHALKED_DB=/data/chalked.sqlite3
CHALKED_CRON_SECRET=generate-a-long-random-secret
CHALKED_MAIL_FROM=Chalked <noreply@your-domain.com>
CHALKED_SMTP_HOST=your-smtp-host
CHALKED_SMTP_PORT=587
CHALKED_SMTP_USERNAME=your-smtp-user
CHALKED_SMTP_PASSWORD=your-smtp-password
CHALKED_SMTP_TLS=1
```

Optional:

```text
CHALKED_SESSION_COOKIE=chalked_session
CHALKED_COOKIE_DOMAIN=.your-domain.com
CHALKED_COOKIE_SECURE=1
CHALKED_ACCESS_LOG=1
```

Do not set `CHALKED_DISABLE_MLB=1` in production.

## Persistent storage

For the private beta, mount a persistent disk at `/data` and set:

```text
CHALKED_DB=/data/chalked.sqlite3
```

Also back up this file regularly. SQLite is good enough for a closed test with a small group, but it should not be the final database for public traffic.

## Scheduled settlement

Production should not rely on users opening the slate page to settle games. Call this endpoint every 2-5 minutes during MLB game windows:

```text
POST https://your-domain.com/api/system/settle
Authorization: Bearer $CHALKED_CRON_SECRET
```

The endpoint checks open slates, syncs MLB live-feed stats, and settles final matchups/picks. If `CHALKED_CRON_SECRET` is not set, the endpoint returns 404.

## Domain setup

1. Deploy the app and confirm the host URL works.
2. Buy or connect `chalked.gg`.
3. Add `chalked.gg` and `www.chalked.gg` as custom domains in the hosting dashboard.
4. Add the DNS records requested by the host in Cloudflare.
5. Verify the domain in the host dashboard.
6. Confirm HTTPS works.
7. Set:

```text
CHALKED_PUBLIC_URL=https://chalked.gg
CHALKED_ALLOWED_ORIGINS=https://chalked.gg,https://www.chalked.gg
```

## Before public launch

- Move database to Postgres.
- Move uploaded profile/league images to object storage.
- Add automated database backups.
- Add observability: uptime checks, error logging, and basic metrics.
- Add rate limiting for auth, uploads, and pick creation.
- Add legal/compliance review before any real-money, prizes, entry fees, or withdrawals.

## Local Docker run

```powershell
docker build -t chalked .
docker run --env-file .env -p 8080:8080 -v chalked-data:/data chalked
```
