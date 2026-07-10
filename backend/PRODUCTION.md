# Chalked Production Setup

This is the private-beta path for turning the local app into a real hosted site.

## Recommended beta stack

- App host: Render Web Service, Railway, Fly.io, or a small VPS/container host
- First smoke test: free Render web service with temporary SQLite
- Database for private beta: SQLite on a persistent disk
- Database for public launch: managed Postgres
- DNS: Cloudflare DNS
- Upload storage: Cloudflare R2 or S3-compatible object storage
- Email: Postmark, SendGrid, Mailgun, or another SMTP provider
- Scheduled jobs: Render Cron Job, GitHub Actions schedule, or host-native cron

## Render-style deploy

The repo includes a `render.yaml` for a private-beta deploy with:

- `chalked`: the web service
- `plan: starter`
- a persistent disk mounted at `/data`
- `CHALKED_PUBLIC_URL=https://chalked.onrender.com`
- persistent SQLite at `/data/chalked.sqlite3`

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

The persistent disk setup requires a paid Render web service. If the blueprint does not attach the disk automatically, add it from the Render dashboard:

- set the web service plan to `starter` or another paid plan
- add a persistent disk named `chalked-data`
- mount it at `/data`
- set `CHALKED_DB=/data/chalked.sqlite3`
- add a cron job that runs `python -m backend.chalked_backend.jobs settle`
- set `CHALKED_PUBLIC_URL` and `CHALKED_CRON_SECRET` on the cron job
- create a Cloudflare R2 bucket for profile and league images

## Required environment

```text
CHALKED_ENV=production
CHALKED_HOST=0.0.0.0
CHALKED_PUBLIC_URL=https://playchalked.com
CHALKED_ALLOWED_ORIGINS=https://playchalked.com,https://www.playchalked.com
CHALKED_DB=/data/chalked.sqlite3
CHALKED_CRON_SECRET=generate-a-long-random-secret
CHALKED_ADMIN_HANDLES=your-admin-username-or-email
CHALKED_REQUIRE_OBJECT_STORAGE=1
CHALKED_MAIL_FROM=Chalked <noreply@playchalked.com>
CHALKED_SMTP_HOST=smtp.postmarkapp.com
CHALKED_SMTP_PORT=587
CHALKED_SMTP_USERNAME=your-postmark-server-token
CHALKED_SMTP_PASSWORD=your-postmark-server-token
CHALKED_SMTP_TLS=1
CHALKED_UPLOAD_BUCKET=chalked-uploads
CHALKED_UPLOAD_ENDPOINT=https://your-account-id.r2.cloudflarestorage.com
CHALKED_UPLOAD_ACCESS_KEY_ID=your-r2-access-key
CHALKED_UPLOAD_SECRET_ACCESS_KEY=your-r2-secret-key
CHALKED_UPLOAD_REGION=auto
CHALKED_UPLOAD_PUBLIC_URL=https://uploads.playchalked.com
CHALKED_UPLOAD_PREFIX=uploads
CHALKED_BACKUP_PREFIX=backups
```

Optional:

```text
CHALKED_SESSION_COOKIE=chalked_session
CHALKED_COOKIE_DOMAIN=.your-domain.com
CHALKED_COOKIE_SECURE=1
CHALKED_ACCESS_LOG=1
```

Do not set `CHALKED_DISABLE_MLB=1` in production.

## Email setup

Chalked sends email verification and password reset messages through SMTP. Postmark is the simplest recommended provider once `playchalked.com` email/domain verification is ready.

Postmark setup:

1. Add and verify `playchalked.com` in Postmark.
2. Add the DNS records Postmark gives you in Cloudflare.
3. Create or open a Postmark Server and copy its Server API Token.
4. In Render, set:

```text
CHALKED_MAIL_FROM=Chalked <noreply@playchalked.com>
CHALKED_SMTP_HOST=smtp.postmarkapp.com
CHALKED_SMTP_PORT=587
CHALKED_SMTP_USERNAME=your-postmark-server-token
CHALKED_SMTP_PASSWORD=your-postmark-server-token
CHALKED_SMTP_TLS=1
```

5. Redeploy. In the in-app Admin tab, the Email card should say `ready`.

Until SMTP is configured, local/dev mode records email in the `email_outbox` table and returns dev links in API responses. Production should use real SMTP before inviting many users.

## Persistent storage

For the private beta, mount a persistent disk at `/data` and set:

```text
CHALKED_DB=/data/chalked.sqlite3
```

Render only preserves files written under the disk mount path, so do not point `CHALKED_DB` anywhere outside `/data` in production. Also back up this file regularly. SQLite is good enough for a closed test with a small group, but it should not be the final database for public traffic.

If the live Render service already has users or leagues on `/tmp/chalked.sqlite3`, do not switch `CHALKED_DB` to `/data/chalked.sqlite3` until you copy the old database. The safest manual migration is:

1. Add the Render disk at `/data` while temporarily keeping `CHALKED_DB=/tmp/chalked.sqlite3`.
2. Open the Render shell and copy `/tmp/chalked.sqlite3` to `/data/chalked.sqlite3`.
3. Set `CHALKED_DB=/data/chalked.sqlite3`.
4. Redeploy and confirm `/api/health` works.

## Object storage for uploads

Local app-server uploads are fine for development, but production profile and league images should live in object storage.

Cloudflare R2 setup:

1. Create an R2 bucket, for example `chalked-uploads`.
2. Create an R2 API token with object read/write access to that bucket.
3. Add a public/custom domain for the bucket, for example `uploads.playchalked.com`.
4. Set these Render env vars:

```text
CHALKED_UPLOAD_BUCKET=chalked-uploads
CHALKED_UPLOAD_ENDPOINT=https://your-account-id.r2.cloudflarestorage.com
CHALKED_UPLOAD_ACCESS_KEY_ID=your-r2-access-key
CHALKED_UPLOAD_SECRET_ACCESS_KEY=your-r2-secret-key
CHALKED_UPLOAD_REGION=auto
CHALKED_UPLOAD_PUBLIC_URL=https://uploads.playchalked.com
CHALKED_UPLOAD_PREFIX=uploads
CHALKED_BACKUP_PREFIX=backups
```

If those env vars are missing, Chalked falls back to local `/uploads/...` storage.

For production, set `CHALKED_REQUIRE_OBJECT_STORAGE=1`. This makes uploads fail loudly if R2/S3 is missing instead of silently writing images to Render's app filesystem.

## Scheduled settlement

Production should not rely on users opening the slate page to settle games. Call this endpoint every 2-5 minutes during MLB game windows:

```text
POST https://your-domain.com/api/system/settle
Authorization: Bearer $CHALKED_CRON_SECRET
```

The endpoint checks open slates, syncs MLB live-feed stats, and settles final matchups/picks. If `CHALKED_CRON_SECRET` is not set, the endpoint returns 404.

Every successful cron call records a `settlement` heartbeat in the database. Admin users can verify the cron freshness from the in-app Admin tab.

## Scheduled backups

Production should also run a daily database backup:

```text
POST https://your-domain.com/api/system/backup
Authorization: Bearer $CHALKED_CRON_SECRET
```

The included `chalked-backup` Render cron job runs `python -m backend.chalked_backend.jobs backup`. It creates a safe SQLite snapshot and uploads it to the same R2/S3 bucket under `CHALKED_BACKUP_PREFIX`, usually `backups`.

Every successful backup records a `backup` heartbeat in the database. Admin users can verify backup freshness from the in-app Admin tab.

## Admin access

Set `CHALKED_ADMIN_HANDLES` to a comma-separated list of admin usernames or emails, for example:

```text
CHALKED_ADMIN_HANDLES=yourname@playchalked.com,yourusername
```

Admin users see an Admin tab with cron freshness, backup freshness, upload storage mode, beta counts, user reports, and account blacklist controls. Blacklisted accounts are signed out and blocked from logging back in.

## Domain setup

1. Deploy the app and confirm the host URL works.
2. Buy or connect `playchalked.com`.
3. Add `playchalked.com` and `www.playchalked.com` as custom domains in the hosting dashboard.
4. Add the DNS records requested by the host in Cloudflare.
5. Verify the domain in the host dashboard.
6. Confirm HTTPS works.
7. Set:

```text
CHALKED_PUBLIC_URL=https://playchalked.com
CHALKED_ALLOWED_ORIGINS=https://playchalked.com,https://www.playchalked.com
```

## Before public launch

- Move database to Postgres.
- Verify uploaded profile/league images are using object storage.
- Confirm automated database backups are fresh and downloadable.
- Add observability: uptime checks, error logging, and basic metrics.
- Expand rate limiting and abuse controls as traffic grows.
- Add legal/compliance review before any real-money, prizes, entry fees, or withdrawals.

## Local Docker run

```powershell
docker build -t chalked .
docker run --env-file .env -p 8080:8080 -v chalked-data:/data chalked
```
