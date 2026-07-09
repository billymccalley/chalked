from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT.parent / "chalked.sqlite3"
SCHEMA = ROOT / "schema.sql"


def db_path() -> Path:
    return Path(os.environ.get("CHALKED_DB", DEFAULT_DB))


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: Path | None = None) -> None:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(target)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        migrate(conn)
        conn.commit()
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    def columns(table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    additions = {
        "users": [
            ("email_verified_at", "TEXT"),
            ("display_name", "TEXT"),
            ("avatar_url", "TEXT"),
            ("last_handle_change_at", "TEXT"),
        ],
        "leagues": [
            ("avatar_url", "TEXT"),
            ("playoff_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("playoff_size", "INTEGER NOT NULL DEFAULT 8"),
            ("season_weeks", "INTEGER NOT NULL DEFAULT 10"),
            ("playoff_weeks", "INTEGER NOT NULL DEFAULT 3"),
        ],
        "memberships": [
            ("display_name", "TEXT"),
            ("avatar_url", "TEXT"),
        ],
        "slates": [
            ("game_date", "TEXT"),
        ],
        "matchups": [
            ("game_pk", "TEXT"),
            ("game_pk_a", "TEXT"),
            ("game_pk_b", "TEXT"),
            ("game_start_a", "TEXT"),
            ("game_start_b", "TEXT"),
            ("game_status_a", "TEXT"),
            ("game_status_b", "TEXT"),
            ("inning_a", "TEXT"),
            ("inning_b", "TEXT"),
            ("live_state_a", "TEXT"),
            ("live_state_b", "TEXT"),
            ("opponent_a", "TEXT"),
            ("opponent_b", "TEXT"),
            ("eligibility_role_a", "TEXT"),
            ("eligibility_role_b", "TEXT"),
            ("eligibility_reason_a", "TEXT"),
            ("eligibility_reason_b", "TEXT"),
            ("eligibility_confidence_a", "TEXT"),
            ("eligibility_confidence_b", "TEXT"),
            ("game_start", "TEXT"),
            ("game_status", "TEXT"),
            ("inning", "TEXT"),
            ("live_state", "TEXT"),
            ("stat_current_a", "REAL"),
            ("stat_current_b", "REAL"),
            ("last5_a", "TEXT"),
            ("last5_b", "TEXT"),
            ("pub_tie", "INTEGER NOT NULL DEFAULT 80"),
            ("stat_source", "TEXT"),
            ("stat_synced_at", "TEXT"),
            ("settled_at", "TEXT"),
        ],
        "sessions": [
            ("user_agent", "TEXT"),
            ("ip_address", "TEXT"),
            ("last_seen_at", "TEXT"),
        ],
    }
    for table, specs in additions.items():
        existing = columns(table)
        for name, ddl in specs:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playoff_matchups (
          id TEXT PRIMARY KEY,
          league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
          round_no INTEGER NOT NULL,
          matchup_no INTEGER NOT NULL,
          week INTEGER NOT NULL,
          seed_a INTEGER,
          seed_b INTEGER,
          user_a_id TEXT REFERENCES users(id),
          user_b_id TEXT REFERENCES users(id),
          winner_user_id TEXT REFERENCES users(id),
          score_a INTEGER NOT NULL DEFAULT 0,
          score_b INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (league_id, round_no, matchup_no)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_login_aliases (
          alias TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_tokens (
          id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          email TEXT,
          purpose TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          expires_at TEXT NOT NULL,
          used_at TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_outbox (
          id TEXT PRIMARY KEY,
          recipient TEXT NOT NULL,
          subject TEXT NOT NULL,
          body TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          error TEXT,
          sent_at TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity_events (
          id TEXT PRIMARY KEY,
          league_id TEXT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
          user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
          kind TEXT NOT NULL,
          message TEXT NOT NULL,
          metadata TEXT,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_playoffs_league ON playoff_matchups(league_id, round_no, matchup_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_league ON activity_events(league_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_tokens_hash ON email_tokens(token_hash)")


@contextmanager
def transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
