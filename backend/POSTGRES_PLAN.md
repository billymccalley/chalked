# Postgres Migration Plan

Chalked currently uses SQLite through `backend/chalked_backend/db.py`. That is fine for local dev and a small private beta with persistent disk. For public traffic, move to managed Postgres.

## Goal

Support:

- Durable hosted database
- Multiple app instances
- Safer concurrent writes
- Backups and point-in-time recovery
- Better reporting and moderation tooling later

## Code changes

1. Add a database driver dependency.
   - Recommended: `psycopg[binary]`
2. Change `db.connect()` to select the backend:
   - `CHALKED_DB=/path/to/chalked.sqlite3` keeps SQLite.
   - `DATABASE_URL=postgres://...` enables Postgres.
3. Split schema into dialect-aware migrations.
   - SQLite uses current `schema.sql`.
   - Postgres needs compatible DDL for auto-increment behavior, JSON/text, timestamps, indexes, and conflict clauses.
4. Audit SQL statements for SQLite-specific syntax.
   - `INSERT OR IGNORE`
   - `ON CONFLICT(...) DO UPDATE`
   - `?` parameter placeholders
   - `PRAGMA foreign_keys`
5. Add a migration runner.
   - Track migrations in a `schema_migrations` table.
   - Run migrations at startup before `ensure_seeded`.
6. Add export/import tooling.
   - Export SQLite rows in dependency order.
   - Import into Postgres inside a transaction.
   - Verify row counts and key league/user/slate checks.

## Operational steps

1. Create managed Postgres in the same region as the app.
2. Run schema migrations.
3. Copy beta data from SQLite to Postgres.
4. Start a staging app against Postgres.
5. Smoke test:
   - Register/login
   - Create/join league
   - Upload profile/league image
   - Refresh slate
   - Lock picks
   - Run `/api/system/settle`
6. Cut production app to `DATABASE_URL`.
7. Keep SQLite backup read-only for rollback.

## Tables to verify after migration

- `users`
- `sessions`
- `leagues`
- `memberships`
- `standings`
- `players`
- `slates`
- `matchups`
- `picks`
- `playoff_matchups`
- `activity_events`
- `email_tokens`
- `email_outbox`

## Do not do this yet

Do not point production at Postgres until automated tests run against both SQLite and Postgres. The current code is SQLite-first.
