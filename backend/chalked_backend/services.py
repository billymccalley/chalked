from __future__ import annotations

import random
import sqlite3
import logging
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import row_to_dict
from .mailer import public_url, send_email, smtp_enabled, smtp_status
from .providers import (
    STAT_RULES,
    MlbLiveFeedProvider,
    MlbGameLogProvider,
    MlbScheduleProvider,
    MlbStatsProvider,
    PlayerEligibility,
    StaticPlayerProvider,
    matchup_line,
    static_schedule,
)
from .security import expires_iso, hash_password, hash_token, make_code, make_token, new_id, now_iso, verify_password


LOGGER = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "bankroll": 1000,
    "min_stake": 10,
    "max_stake": 500,
    "min_mult": 1.2,
    "max_mult": 4.0,
    "streak_step": 10,
    "streak_cap": 50,
    "margin_bonus": 0.25,
    "matchups_per_slate": 12,
    "drift": 70,
    "playoff_enabled": 1,
    "playoff_size": 8,
    "season_weeks": 10,
    "playoff_weeks": 3,
}

PLAYOFF_SIZES = {4, 6, 8, 10, 12, 14, 16}
HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{3,20}$")

BOT_HANDLES = ["MoonshotMaria", "K_Machine", "BackdoorSlider", "CheeseAt99", "RallyCapRandy"]
PITCHER_STAT_GROUPS = ("K", "BF", "IP")
BATTER_STAT_GROUPS = ("TB", "OB", "H", "R", "RBI", "SPD", "HR", "XBH", "HHR", "BB")
_SCHEDULE_CACHE: dict[str, tuple[datetime, list]] = {}
_LIVE_FEED_CACHE: dict[str, tuple[datetime, dict | None]] = {}
_GAME_LOG_CACHE: dict[tuple[str, str, int], tuple[datetime, list[dict]]] = {}
_SLATE_SYNC_CACHE: dict[str, datetime] = {}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def public_user(row: sqlite3.Row | dict) -> dict[str, Any]:
    return {
        "id": row["id"],
        "handle": row["handle"],
        "email": row["email"],
        "display_name": row["display_name"] or row["handle"],
        "avatar_url": row["avatar_url"],
        "email_verified_at": row["email_verified_at"] if "email_verified_at" in row.keys() else None,
        "last_handle_change_at": row["last_handle_change_at"] if "last_handle_change_at" in row.keys() else None,
        "terms_accepted_at": row["terms_accepted_at"] if "terms_accepted_at" in row.keys() else None,
        "privacy_accepted_at": row["privacy_accepted_at"] if "privacy_accepted_at" in row.keys() else None,
        "is_admin": is_admin_user(row),
    }


def require_fields(data: dict, *fields: str) -> None:
    missing = [f for f in fields if not data.get(f)]
    if missing:
        raise ApiError(400, f"Missing required field: {', '.join(missing)}")


def clean_handle(value: object) -> str:
    handle = str(value or "").strip()
    if not HANDLE_RE.fullmatch(handle):
        raise ApiError(400, "Username must be 3-20 characters and can only use letters, numbers, dots, underscores, or hyphens")
    return handle


def demo_seed_enabled() -> bool:
    return os.getenv("CHALKED_DEMO_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def admin_identifiers() -> set[str]:
    raw = os.getenv("CHALKED_ADMIN_HANDLES", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def is_admin_user(row: sqlite3.Row | dict | None) -> bool:
    if not row:
        return False
    admins = admin_identifiers()
    handle = str(row["handle"] or "").lower()
    email = str(row["email"] or "").lower()
    return bool(admins and (handle in admins or email in admins))


def require_admin(user: sqlite3.Row | dict) -> None:
    if not is_admin_user(user):
        raise ApiError(403, "Admin access required")


def moderation_status(conn: sqlite3.Connection, user_id: str) -> str | None:
    row = conn.execute("SELECT status FROM user_moderation WHERE user_id = ?", (user_id,)).fetchone()
    return row["status"] if row else None


def require_account_allowed(conn: sqlite3.Connection, user_id: str) -> None:
    if moderation_status(conn, user_id) == "blacklisted":
        raise ApiError(403, "This account has been blacklisted")


def ensure_seeded(conn: sqlite3.Connection, sync_players: bool = False) -> None:
    static_players = list(StaticPlayerProvider().players())
    player_count = conn.execute("SELECT COUNT(*) c FROM players WHERE active = 1").fetchone()["c"]
    demo_exists = conn.execute("SELECT 1 FROM users WHERE handle = ? OR email = ?", ("demo", "demo@chalked.local")).fetchone()
    default_league = conn.execute("SELECT code, name FROM leagues WHERE code = ? OR lower(name) = ?", ("CHALK", "chalked")).fetchone()
    old_clubhouse = conn.execute("SELECT 1 FROM leagues WHERE code IN (?, ?) OR lower(name) = ?", ("HOME", "RJRXI1M", "the clubhouse")).fetchone()
    default_league_ready = bool(default_league and default_league["code"] == "CHALK" and default_league["name"] == "Chalked" and not old_clubhouse)
    bot_placeholders = ",".join("?" for _ in BOT_HANDLES)
    bot_count = conn.execute(f"SELECT COUNT(*) c FROM users WHERE handle IN ({bot_placeholders})", BOT_HANDLES).fetchone()["c"]
    if not sync_players and player_count >= len(static_players) and demo_exists and default_league_ready and (not demo_seed_enabled() or bot_count == len(BOT_HANDLES)):
        return

    if sync_players or player_count < len(static_players):
        players = static_players
        if sync_players:
            try:
                players = list(MlbStatsProvider().players())
            except RuntimeError:
                players = static_players
        for p in players:
            conn.execute(
                """
                INSERT INTO players (id, external_id, name, team, position, stat_group, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  external_id = excluded.external_id,
                  name = excluded.name,
                  team = excluded.team,
                  position = excluded.position,
                  stat_group = excluded.stat_group,
                  active = 1,
                  updated_at = excluded.updated_at
                """,
                (p.id, p.external_id, p.name, p.team, p.position, p.stat_group, now_iso()),
            )
    if demo_seed_enabled():
        for handle in BOT_HANDLES:
            if not conn.execute("SELECT 1 FROM users WHERE handle = ?", (handle,)).fetchone():
                create_user(conn, {"handle": handle, "email": f"{handle.lower()}@bots.chalked.local", "password": new_id("bot")}, bot=True)
    demo_user = conn.execute("SELECT * FROM users WHERE handle = ? OR email = ?", ("demo", "demo@chalked.local")).fetchone()
    if not demo_user:
        demo_user = create_user(conn, {"handle": "demo", "email": "demo@chalked.local", "password": "demo12345"})
    default_description = "The main public Chalked league."
    target_league = conn.execute(
        """
        SELECT * FROM leagues
        WHERE code = ? OR lower(name) = ?
        ORDER BY code = ? DESC, created_at
        LIMIT 1
        """,
        ("CHALK", "chalked", "CHALK"),
    ).fetchone()
    if target_league:
        conn.execute(
            "UPDATE leagues SET code = ?, name = ?, description = ?, visibility = 'open' WHERE id = ?",
            ("CHALK", "Chalked", default_description, target_league["id"]),
        )
        league_id = target_league["id"]
    else:
        user = row_to_dict(demo_user)
        league = create_league(conn, user["id"], {"name": "Chalked", "description": default_description, "code": "CHALK", "visibility": "open"})
        league_id = league["id"]
    conn.execute(
        """
        DELETE FROM leagues
        WHERE id <> ? AND (code IN (?, ?) OR lower(name) = ?)
        """,
        (league_id, "HOME", "RJRXI1M", "the clubhouse"),
    )
    if demo_seed_enabled():
        for bot in conn.execute("SELECT id FROM users WHERE email LIKE '%@bots.chalked.local'").fetchall():
            join_league(conn, bot["id"], league_id)
    ensure_active_slate(conn, league_id)


def create_user(conn: sqlite3.Connection, data: dict, bot: bool = False) -> dict:
    require_fields(data, "handle", "password")
    user_id = new_id("usr")
    handle = clean_handle(data["handle"])
    email = str(data.get("email") or "").strip().lower() or None
    accepted_at = now_iso() if data.get("accept_terms") or data.get("terms_accepted") or bot else None
    existing = conn.execute(
        "SELECT 1 FROM users WHERE lower(handle) = ? OR (? IS NOT NULL AND lower(email) = ?)",
        (handle.lower(), email, email),
    ).fetchone()
    if existing:
        if bot:
            return row_to_dict(conn.execute("SELECT * FROM users WHERE lower(handle) = ?", (handle.lower(),)).fetchone())
        raise ApiError(409, "Username or email is already taken")
    try:
        conn.execute(
            """
            INSERT INTO users (
              id, handle, email, display_name, avatar_url, terms_accepted_at, privacy_accepted_at, password_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                handle,
                email,
                data.get("display_name") or handle,
                data.get("avatar_url"),
                accepted_at,
                accepted_at,
                hash_password(data["password"]),
                now_iso(),
            ),
        )
    except sqlite3.IntegrityError:
        if bot:
            return row_to_dict(conn.execute("SELECT * FROM users WHERE handle = ?", (data["handle"],)).fetchone())
        raise ApiError(409, "Handle or email is already taken")
    return row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def login(conn: sqlite3.Connection, data: dict, meta: dict | None = None) -> tuple[dict, str]:
    identifier = str(data.get("login") or data.get("handle") or data.get("email") or "").strip()
    if not identifier or not data.get("password"):
        raise ApiError(400, "Missing required field: username/email, password")
    normalized = identifier.lower()
    user = conn.execute(
        "SELECT * FROM users WHERE lower(handle) = ? OR lower(email) = ?",
        (normalized, normalized),
    ).fetchone()
    if not user or not verify_password(data["password"], user["password_hash"]):
        raise ApiError(401, "Invalid username/email or password")
    require_account_allowed(conn, user["id"])
    session_id = new_id("ses")
    meta = meta or {}
    conn.execute(
        "INSERT INTO sessions (id, user_id, expires_at, user_agent, ip_address, last_seen_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            user["id"],
            expires_iso(),
            str(meta.get("user_agent") or "")[:240] or None,
            str(meta.get("ip_address") or "")[:80] or None,
            now_iso(),
            now_iso(),
        ),
    )
    return public_user(user), session_id


def user_from_session(conn: sqlite3.Connection, session_id: str | None) -> dict | None:
    if not session_id:
        return None
    row = conn.execute(
        """
        SELECT u.* FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.id = ? AND s.expires_at > ?
        """,
        (session_id, now_iso()),
    ).fetchone()
    if row:
        require_account_allowed(conn, row["id"])
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now_iso(), session_id))
    return row_to_dict(row)


def session_rows(conn: sqlite3.Connection, user_id: str, current_session_id: str | None = None) -> dict:
    rows = conn.execute(
        """
        SELECT id, expires_at, user_agent, ip_address, last_seen_at, created_at
        FROM sessions
        WHERE user_id = ? AND expires_at > ?
        ORDER BY COALESCE(last_seen_at, created_at) DESC
        """,
        (user_id, now_iso()),
    ).fetchall()
    return {
        "sessions": [
            {
                "id": r["id"],
                "current": bool(current_session_id and r["id"] == current_session_id),
                "expires_at": r["expires_at"],
                "user_agent": r["user_agent"],
                "ip_address": r["ip_address"],
                "last_seen_at": r["last_seen_at"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


def logout_other_sessions(conn: sqlite3.Connection, user_id: str, current_session_id: str | None) -> dict:
    if current_session_id:
        removed = conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND id != ?",
            (user_id, current_session_id),
        ).rowcount
    else:
        removed = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
    return {"ok": True, "removed": removed}


def profile(conn: sqlite3.Connection, user_id: str) -> dict:
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    aliases = conn.execute(
        """
        SELECT m.league_id, l.name league_name, m.display_name, m.avatar_url
        FROM memberships m
        JOIN leagues l ON l.id = m.league_id
        WHERE m.user_id = ?
        ORDER BY l.created_at
        """,
        (user_id,),
    ).fetchall()
    return {
        "user": public_user(user),
        "league_profiles": [
            {
                "league_id": r["league_id"],
                "league_name": r["league_name"],
                "display_name": r["display_name"] or user["display_name"] or user["handle"],
                "avatar_url": r["avatar_url"] or user["avatar_url"],
            }
            for r in aliases
        ],
    }


def change_password(conn: sqlite3.Connection, user_id: str, data: dict, current_session_id: str | None = None) -> dict:
    current_password = str(data.get("current_password") or "")
    new_password = str(data.get("new_password") or "")
    if not current_password or not new_password:
        raise ApiError(400, "Current password and new password are required")
    if len(new_password) < 8:
        raise ApiError(400, "New password must be at least 8 characters")
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user or not verify_password(current_password, user["password_hash"]):
        raise ApiError(401, "Current password is incorrect")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user_id))
    if current_session_id:
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (user_id, current_session_id))
    return {"ok": True}


def create_email_token(conn: sqlite3.Connection, user_id: str, email: str | None, purpose: str, minutes: int) -> str:
    token = make_token()
    conn.execute(
        """
        INSERT INTO email_tokens (id, user_id, email, purpose, token_hash, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("tok"), user_id, email, purpose, hash_token(token), (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(), now_iso()),
    )
    return token


def request_email_verification(conn: sqlite3.Connection, user_id: str) -> dict:
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        raise ApiError(404, "User not found")
    if not user["email"]:
        raise ApiError(400, "Add an email before verifying it")
    if user["email_verified_at"]:
        return {"ok": True, "already_verified": True}
    token = create_email_token(conn, user_id, user["email"], "verify_email", 60 * 24)
    link = f"{public_url()}/?verify={token}"
    send_email(
        conn,
        user["email"],
        "Verify your Chalked email",
        f"Welcome to Chalked.\n\nVerify your email here:\n{link}\n\nThis link expires in 24 hours.",
    )
    return {"ok": True, "dev_link": None if smtp_enabled() else link}


def confirm_email_verification(conn: sqlite3.Connection, token: str) -> dict:
    row = conn.execute(
        """
        SELECT * FROM email_tokens
        WHERE token_hash = ? AND purpose = 'verify_email' AND used_at IS NULL AND expires_at > ?
        """,
        (hash_token(str(token or "")), now_iso()),
    ).fetchone()
    if not row:
        raise ApiError(400, "Verification link is invalid or expired")
    conn.execute(
        "UPDATE users SET email_verified_at = ? WHERE id = ? AND lower(email) = ?",
        (now_iso(), row["user_id"], str(row["email"] or "").lower()),
    )
    conn.execute("UPDATE email_tokens SET used_at = ? WHERE id = ?", (now_iso(), row["id"]))
    user = conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    return {"ok": True, "user": public_user(user)}


def request_password_reset(conn: sqlite3.Connection, data: dict) -> dict:
    identifier = str(data.get("login") or data.get("email") or "").strip().lower()
    if not identifier:
        raise ApiError(400, "Email or username is required")
    user = conn.execute(
        "SELECT * FROM users WHERE lower(handle) = ? OR lower(email) = ?",
        (identifier, identifier),
    ).fetchone()
    if user and user["email"]:
        token = create_email_token(conn, user["id"], user["email"], "password_reset", 60)
        link = f"{public_url()}/?reset={token}"
        send_email(
            conn,
            user["email"],
            "Reset your Chalked password",
            f"Reset your Chalked password here:\n{link}\n\nThis link expires in 1 hour. If you did not ask for it, ignore this email.",
        )
        return {"ok": True, "dev_link": None if smtp_enabled() else link}
    return {"ok": True}


def confirm_password_reset(conn: sqlite3.Connection, data: dict) -> dict:
    token = str(data.get("token") or "")
    new_password = str(data.get("new_password") or "")
    if len(new_password) < 8:
        raise ApiError(400, "New password must be at least 8 characters")
    row = conn.execute(
        """
        SELECT * FROM email_tokens
        WHERE token_hash = ? AND purpose = 'password_reset' AND used_at IS NULL AND expires_at > ?
        """,
        (hash_token(token), now_iso()),
    ).fetchone()
    if not row:
        raise ApiError(400, "Reset link is invalid or expired")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), row["user_id"]))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
    conn.execute("UPDATE email_tokens SET used_at = ? WHERE id = ?", (now_iso(), row["id"]))
    return {"ok": True}


def log_activity(conn: sqlite3.Connection, league_id: str, user_id: str | None, kind: str, message: str, metadata: dict | None = None) -> None:
    conn.execute(
        """
        INSERT INTO activity_events (id, league_id, user_id, kind, message, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("act"), league_id, user_id, kind, message, json.dumps(metadata or {}), now_iso()),
    )


def activity_feed(conn: sqlite3.Connection, user_id: str, league_id: str, limit: int = 30) -> dict:
    require_member(conn, user_id, league_id)
    rows = conn.execute(
        """
        SELECT a.*, u.handle, u.display_name, u.avatar_url
        FROM activity_events a
        LEFT JOIN users u ON u.id = a.user_id
        WHERE a.league_id = ?
        ORDER BY a.created_at DESC
        LIMIT ?
        """,
        (league_id, max(1, min(int(limit), 75))),
    ).fetchall()
    return {
        "events": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "message": r["message"],
                "created_at": r["created_at"],
                "user": {
                    "id": r["user_id"],
                    "handle": r["handle"],
                    "display_name": r["display_name"] or r["handle"],
                    "avatar_url": r["avatar_url"],
                } if r["user_id"] else None,
                "metadata": json.loads(r["metadata"] or "{}"),
            }
            for r in rows
        ]
    }


def _require_matchup_in_league(conn: sqlite3.Connection, league_id: str, matchup_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT m.id, m.slate_id
        FROM matchups m
        JOIN slates s ON s.id = m.slate_id
        WHERE m.id = ? AND s.league_id = ?
        """,
        (matchup_id, league_id),
    ).fetchone()
    if not row:
        raise ApiError(404, "Matchup not found")
    return row


def chat_message_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "league_id": row["league_id"],
        "slate_id": row["slate_id"],
        "matchup_id": row["matchup_id"],
        "user_id": row["user_id"],
        "message": row["message"],
        "created_at": row["created_at"],
        "user": {
            "id": row["user_id"],
            "handle": row["handle"],
            "display_name": row["display_name"] or row["handle"] or "Deleted user",
            "avatar_url": row["avatar_url"],
        } if row["user_id"] else None,
    }


def matchup_chat_messages(conn: sqlite3.Connection, matchup_id: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.*,
               u.handle,
               COALESCE(mem.display_name, u.display_name, u.handle) AS display_name,
               COALESCE(mem.avatar_url, u.avatar_url) AS avatar_url
        FROM matchup_chat_messages c
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN memberships mem ON mem.league_id = c.league_id AND mem.user_id = c.user_id
        WHERE c.matchup_id = ?
        ORDER BY c.created_at ASC
        LIMIT ?
        """,
        (matchup_id, max(1, min(int(limit), 100))),
    ).fetchall()
    return [chat_message_dict(row) for row in rows]


def matchup_chat(conn: sqlite3.Connection, user_id: str, league_id: str, matchup_id: str, limit: int = 60) -> dict:
    require_member(conn, user_id, league_id)
    _require_matchup_in_league(conn, league_id, matchup_id)
    return {"messages": matchup_chat_messages(conn, matchup_id, limit)}


def create_matchup_chat(conn: sqlite3.Connection, user_id: str, league_id: str, matchup_id: str, data: dict) -> dict:
    require_member(conn, user_id, league_id)
    matchup = _require_matchup_in_league(conn, league_id, matchup_id)
    message = re.sub(r"\s+", " ", str(data.get("message") or data.get("txt") or "")).strip()
    if not message:
        raise ApiError(400, "Message cannot be empty")
    if len(message) > 280:
        raise ApiError(400, "Message must be 280 characters or less")
    message_id = new_id("chat")
    created_at = now_iso()
    conn.execute(
        """
        INSERT INTO matchup_chat_messages (id, league_id, slate_id, matchup_id, user_id, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (message_id, league_id, matchup["slate_id"], matchup_id, user_id, message, created_at),
    )
    row = conn.execute(
        """
        SELECT c.*,
               u.handle,
               COALESCE(mem.display_name, u.display_name, u.handle) AS display_name,
               COALESCE(mem.avatar_url, u.avatar_url) AS avatar_url
        FROM matchup_chat_messages c
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN memberships mem ON mem.league_id = c.league_id AND mem.user_id = c.user_id
        WHERE c.id = ?
        """,
        (message_id,),
    ).fetchone()
    return chat_message_dict(row)


def record_system_status(conn: sqlite3.Connection, key: str, value: dict) -> dict:
    stamp = now_iso()
    payload = json.dumps(value, sort_keys=True)
    conn.execute(
        """
        INSERT INTO system_status (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, payload, stamp),
    )
    return {"key": key, "value": value, "updated_at": stamp}


def system_status(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM system_status WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    return {"key": row["key"], "value": json.loads(row["value"] or "{}"), "updated_at": row["updated_at"]}


def check_rate_limit(conn: sqlite3.Connection, key: str, route: str, limit: int, window_seconds: int) -> dict:
    now = datetime.now(timezone.utc)
    row = conn.execute("SELECT * FROM rate_limits WHERE key = ? AND route = ?", (key, route)).fetchone()
    if row:
        reset_at = parse_utc(row["reset_at"])
        if reset_at > now:
            count = int(row["count"]) + 1
            conn.execute("UPDATE rate_limits SET count = ? WHERE key = ? AND route = ?", (count, key, route))
            if count > limit:
                retry_after = max(1, int((reset_at - now).total_seconds()))
                raise ApiError(429, f"Too many requests. Try again in {retry_after} seconds.")
            return {"limited": False, "count": count, "reset_at": row["reset_at"]}
    reset_at = (now + timedelta(seconds=window_seconds)).isoformat()
    conn.execute(
        """
        INSERT INTO rate_limits (key, route, count, reset_at)
        VALUES (?, ?, 1, ?)
        ON CONFLICT(key, route) DO UPDATE SET count = 1, reset_at = excluded.reset_at
        """,
        (key, route, reset_at),
    )
    conn.execute("DELETE FROM rate_limits WHERE reset_at < ?", ((now - timedelta(minutes=5)).isoformat(),))
    return {"limited": False, "count": 1, "reset_at": reset_at}


def create_feedback_report(conn: sqlite3.Connection, user_id: str | None, data: dict, meta: dict | None = None) -> dict:
    message = str(data.get("message") or "").strip()
    if len(message) < 8:
        raise ApiError(400, "Tell us a little more before sending.")
    if len(message) > 2000:
        raise ApiError(400, "Feedback must be under 2000 characters.")
    category = str(data.get("category") or "general").strip().lower()[:40] or "general"
    if not re.fullmatch(r"[a-z0-9_-]+", category):
        category = "general"
    meta = meta or {}
    report_id = new_id("fbk")
    conn.execute(
        """
        INSERT INTO feedback_reports (id, user_id, category, message, page_url, user_agent, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            report_id,
            user_id,
            category,
            message,
            str(data.get("page_url") or "")[:500] or None,
            str(meta.get("user_agent") or "")[:500] or None,
            str(meta.get("ip_address") or "")[:120] or None,
            now_iso(),
        ),
    )
    return {"id": report_id, "status": "received"}


def latest_feedback(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        """
        SELECT f.*, u.handle, u.display_name
        FROM feedback_reports f
        LEFT JOIN users u ON u.id = f.user_id
        ORDER BY f.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "category": r["category"],
            "message": r["message"],
            "page_url": r["page_url"],
            "status": r["status"],
            "created_at": r["created_at"],
            "user": {
                "id": r["user_id"],
                "handle": r["handle"],
                "display_name": r["display_name"] or r["handle"],
            }
            if r["user_id"]
            else None,
        }
        for r in rows
    ]


def admin_overview(conn: sqlite3.Connection, user_id: str) -> dict:
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    require_admin(user)
    counts = {
        "users": conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "leagues": conn.execute("SELECT COUNT(*) c FROM leagues").fetchone()["c"],
        "open_slates": conn.execute("SELECT COUNT(*) c FROM slates WHERE status = 'open'").fetchone()["c"],
        "open_picks": conn.execute("SELECT COUNT(*) c FROM picks WHERE status = 'open'").fetchone()["c"],
        "blacklisted": conn.execute("SELECT COUNT(*) c FROM user_moderation WHERE status = 'blacklisted'").fetchone()["c"],
        "open_feedback": conn.execute("SELECT COUNT(*) c FROM feedback_reports WHERE status = 'open'").fetchone()["c"],
    }
    users = conn.execute(
        """
        SELECT u.id, u.handle, u.email, u.display_name, u.created_at,
               COALESCE(m.status, 'active') moderation_status, m.reason moderation_reason, m.updated_at moderation_updated_at,
               (SELECT COUNT(*) FROM memberships WHERE user_id = u.id) league_count,
               (SELECT COUNT(*) FROM picks WHERE user_id = u.id) pick_count
        FROM users u
        LEFT JOIN user_moderation m ON m.user_id = u.id
        ORDER BY CASE WHEN m.status = 'blacklisted' THEN 0 ELSE 1 END, u.created_at DESC
        LIMIT 100
        """
    ).fetchall()
    last_mail = conn.execute(
        """
        SELECT recipient, subject, status, error, sent_at, created_at
        FROM email_outbox
        ORDER BY created_at DESC
        LIMIT 1
        """
    ).fetchone()
    return {
        "counts": counts,
        "cron": system_status(conn, "settlement"),
        "backup": system_status(conn, "backup"),
        "email": {
            **smtp_status(),
            "last": row_to_dict(last_mail) if last_mail else None,
        },
        "feedback": latest_feedback(conn),
        "users": [
            {
                "id": r["id"],
                "handle": r["handle"],
                "email": r["email"],
                "display_name": r["display_name"] or r["handle"],
                "created_at": r["created_at"],
                "moderation_status": r["moderation_status"],
                "moderation_reason": r["moderation_reason"],
                "moderation_updated_at": r["moderation_updated_at"],
                "league_count": r["league_count"],
                "pick_count": r["pick_count"],
                "is_admin": is_admin_user(r),
            }
            for r in users
        ],
    }


def set_user_moderation(conn: sqlite3.Connection, admin_id: str, target_user_id: str, status: str, reason: str | None = None) -> dict:
    admin = conn.execute("SELECT * FROM users WHERE id = ?", (admin_id,)).fetchone()
    require_admin(admin)
    target = conn.execute("SELECT * FROM users WHERE id = ?", (target_user_id,)).fetchone()
    if not target:
        raise ApiError(404, "User not found")
    if target_user_id == admin_id and status == "blacklisted":
        raise ApiError(400, "You cannot blacklist your own account")
    if is_admin_user(target) and status == "blacklisted":
        raise ApiError(400, "Admin accounts cannot be blacklisted")
    if status not in {"active", "blacklisted"}:
        raise ApiError(400, "Invalid moderation status")
    if status == "active":
        conn.execute("DELETE FROM user_moderation WHERE user_id = ?", (target_user_id,))
    else:
        conn.execute(
            """
            INSERT INTO user_moderation (user_id, status, reason, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              status = excluded.status,
              reason = excluded.reason,
              updated_by = excluded.updated_by,
              updated_at = excluded.updated_at
            """,
            (target_user_id, status, (reason or "Blacklisted by admin").strip()[:240], admin_id, now_iso()),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (target_user_id,))
    return {"user_id": target_user_id, "status": status}


def update_profile(conn: sqlite3.Connection, user_id: str, data: dict) -> dict:
    allowed = {}
    current = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not current:
        raise ApiError(404, "User not found")
    if "handle" in data:
        handle = clean_handle(data["handle"])
        if handle.lower() != str(current["handle"]).lower():
            last_change = current["last_handle_change_at"]
            if last_change:
                changed_at = parse_utc(last_change)
                available_at = changed_at + timedelta(days=7)
                if datetime.now(timezone.utc) < available_at:
                    raise ApiError(429, f"You can change your username again on {available_at.date().isoformat()}")
            existing = conn.execute(
                "SELECT 1 FROM users WHERE lower(handle) = ? AND id != ?",
                (handle.lower(), user_id),
            ).fetchone()
            if existing:
                raise ApiError(409, "Username is already taken")
            allowed["handle"] = handle
            allowed["last_handle_change_at"] = now_iso()
    if "email" in data:
        email = str(data["email"]).strip().lower()
        if email:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE lower(email) = ? AND id != ?",
                (email, user_id),
            ).fetchone()
            if existing:
                raise ApiError(409, "Email is already taken")
        allowed["email"] = email or None
        if (email or None) != current["email"]:
            allowed["email_verified_at"] = None
    if "display_name" in data:
        allowed["display_name"] = str(data["display_name"]).strip()[:40] or None
    if "avatar_url" in data:
        allowed["avatar_url"] = str(data["avatar_url"]).strip()[:500] or None
    if not allowed:
        raise ApiError(400, "No profile fields provided")
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    try:
        conn.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*allowed.values(), user_id))
    except sqlite3.IntegrityError:
        raise ApiError(409, "Username or email is already taken")
    return profile(conn, user_id)


def update_league_profile(conn: sqlite3.Connection, user_id: str, league_id: str, data: dict) -> dict:
    require_member(conn, user_id, league_id)
    allowed = {}
    if "display_name" in data:
        allowed["display_name"] = str(data["display_name"]).strip()[:40] or None
    if "avatar_url" in data:
        allowed["avatar_url"] = str(data["avatar_url"]).strip()[:500] or None
    if not allowed:
        raise ApiError(400, "No league profile fields provided")
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    conn.execute(f"UPDATE memberships SET {assignments} WHERE league_id = ? AND user_id = ?", (*allowed.values(), league_id, user_id))
    return profile(conn, user_id)


def league_dict(conn: sqlite3.Connection, row: sqlite3.Row, user_id: str | None = None) -> dict:
    member_count = conn.execute("SELECT COUNT(*) c FROM memberships WHERE league_id = ?", (row["id"],)).fetchone()["c"]
    mine = False
    role = None
    if user_id:
        m = conn.execute(
            "SELECT role, display_name, avatar_url FROM memberships WHERE league_id = ? AND user_id = ?",
            (row["id"], user_id),
        ).fetchone()
        mine = bool(m)
        role = m["role"] if m else None
    settings = {k: row[k] for k in DEFAULT_SETTINGS}
    playoff_weeks = int(row["playoff_weeks"])
    season_weeks = int(row["season_weeks"])
    return {
        "id": row["id"],
        "code": row["code"] if mine else None,
        "name": row["name"],
        "description": row["description"],
        "owner_id": row["owner_id"],
        "visibility": row["visibility"],
        "avatar_url": row["avatar_url"],
        "member_count": member_count,
        "is_member": mine,
        "role": role,
        "settings": settings,
        "my_profile": {
            "display_name": m["display_name"] if user_id and m else None,
            "avatar_url": m["avatar_url"] if user_id and m else None,
        },
        "playoffs": {
            "enabled": bool(row["playoff_enabled"]),
            "size": int(row["playoff_size"]),
            "season_weeks": season_weeks,
            "playoff_weeks": playoff_weeks,
            "starts_week": season_weeks + 1,
            "championship_week": season_weeks + playoff_weeks,
        },
    }


def list_leagues(conn: sqlite3.Connection, user_id: str) -> dict:
    mine = conn.execute(
        """
        SELECT l.* FROM leagues l
        JOIN memberships m ON m.league_id = l.id
        WHERE m.user_id = ?
        ORDER BY l.created_at
        """,
        (user_id,),
    ).fetchall()
    open_rows = conn.execute(
        """
        SELECT * FROM leagues
        WHERE visibility = 'open'
        AND id NOT IN (SELECT league_id FROM memberships WHERE user_id = ?)
        ORDER BY created_at
        """,
        (user_id,),
    ).fetchall()
    return {
        "mine": [league_dict(conn, row, user_id) for row in mine],
        "open": [league_dict(conn, row, user_id) for row in open_rows],
    }


def create_league(conn: sqlite3.Connection, owner_id: str, data: dict) -> dict:
    require_fields(data, "name")
    league_id = new_id("lg")
    settings = {**DEFAULT_SETTINGS, **{k: data[k] for k in DEFAULT_SETTINGS if k in data}}
    validate_league_settings(settings)
    code = re.sub(r"[^A-Z0-9]", "", str(data.get("code") or make_code()).upper())[:12]
    if len(code) < 4:
        raise ApiError(400, "League code must be at least 4 letters or numbers")
    visibility = str(data.get("visibility") or "open").strip().lower()
    if visibility not in {"open", "private"}:
        raise ApiError(400, "League visibility must be open or private")
    if conn.execute("SELECT 1 FROM leagues WHERE code = ?", (code,)).fetchone():
        raise ApiError(409, "League code is already taken")
    conn.execute(
        """
        INSERT INTO leagues (
          id, code, name, description, owner_id, visibility, bankroll, min_stake, max_stake,
          min_mult, max_mult, streak_step, streak_cap, margin_bonus, matchups_per_slate, drift,
          avatar_url, playoff_enabled, playoff_size, season_weeks, playoff_weeks, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            league_id,
            code,
            data["name"].strip(),
            data.get("description", "A Chalked league."),
            owner_id,
            visibility,
            settings["bankroll"],
            settings["min_stake"],
            settings["max_stake"],
            settings["min_mult"],
            settings["max_mult"],
            settings["streak_step"],
            settings["streak_cap"],
            settings["margin_bonus"],
            settings["matchups_per_slate"],
            settings["drift"],
            data.get("avatar_url"),
            int(settings["playoff_enabled"]),
            int(settings["playoff_size"]),
            int(settings["season_weeks"]),
            int(settings["playoff_weeks"]),
            now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO memberships (league_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
        (league_id, owner_id, now_iso()),
    )
    conn.execute(
        "INSERT INTO standings (league_id, user_id, updated_at) VALUES (?, ?, ?)",
        (league_id, owner_id, now_iso()),
    )
    log_activity(conn, league_id, owner_id, "league_created", f"{data['name'].strip()} was created")
    ensure_active_slate(conn, league_id)
    return league_dict(conn, conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone(), owner_id)


def join_league(conn: sqlite3.Connection, user_id: str, league_id_or_code: str) -> dict:
    league = conn.execute("SELECT * FROM leagues WHERE id = ? OR code = ?", (league_id_or_code, league_id_or_code.upper())).fetchone()
    if not league:
        raise ApiError(404, "League not found")
    membership_result = conn.execute(
        "INSERT OR IGNORE INTO memberships (league_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
        (league["id"], user_id, now_iso()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO standings (league_id, user_id, updated_at) VALUES (?, ?, ?)",
        (league["id"], user_id, now_iso()),
    )
    if membership_result.rowcount:
        user = conn.execute("SELECT display_name, handle FROM users WHERE id = ?", (user_id,)).fetchone()
        log_activity(conn, league["id"], user_id, "member_joined", f"{user['display_name'] or user['handle']} joined the league")
    return league_dict(conn, league, user_id)


def require_member(conn: sqlite3.Connection, user_id: str, league_id: str) -> sqlite3.Row:
    membership = conn.execute(
        "SELECT * FROM memberships WHERE league_id = ? AND user_id = ?",
        (league_id, user_id),
    ).fetchone()
    if not membership:
        raise ApiError(403, "Join this league first")
    return membership


def update_settings(conn: sqlite3.Connection, user_id: str, league_id: str, data: dict) -> dict:
    membership = require_member(conn, user_id, league_id)
    if membership["role"] != "owner":
        raise ApiError(403, "Only the league owner can change settings")
    allowed = {k: data[k] for k in DEFAULT_SETTINGS if k in data}
    if "name" in data:
        name = str(data["name"]).strip()[:60]
        if len(name) < 2:
            raise ApiError(400, "League name must be at least 2 characters")
        allowed["name"] = name
    if "description" in data:
        allowed["description"] = str(data["description"]).strip()[:180] or "A Chalked league."
    if "avatar_url" in data:
        allowed["avatar_url"] = str(data["avatar_url"]).strip()[:500] or None
    if not allowed:
        raise ApiError(400, "No settings provided")
    if "min_stake" in allowed and "max_stake" in allowed and int(allowed["min_stake"]) > int(allowed["max_stake"]):
        raise ApiError(400, "Minimum stake cannot exceed maximum stake")
    row = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if not row:
        raise ApiError(404, "League not found")
    merged = {k: (allowed[k] if k in allowed else row[k]) for k in DEFAULT_SETTINGS}
    validate_league_settings(merged)
    assignments = ", ".join(f"{k} = ?" for k in allowed)
    conn.execute(f"UPDATE leagues SET {assignments} WHERE id = ?", (*allowed.values(), league_id))
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    return league_dict(conn, league, user_id)


def delete_league(conn: sqlite3.Connection, user_id: str, league_id: str) -> dict:
    membership = require_member(conn, user_id, league_id)
    if membership["role"] != "owner":
        raise ApiError(403, "Only the league owner can delete this league")
    league = conn.execute("SELECT id, name FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if not league:
        raise ApiError(404, "League not found")
    conn.execute("DELETE FROM leagues WHERE id = ?", (league_id,))
    return {"deleted": league_id, "name": league["name"]}


def leave_league(conn: sqlite3.Connection, user_id: str, league_id: str) -> dict:
    membership = require_member(conn, user_id, league_id)
    league = conn.execute("SELECT id, name FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if not league:
        raise ApiError(404, "League not found")
    if membership["role"] == "owner":
        raise ApiError(400, "Owners must delete the league or transfer ownership before leaving")
    user = conn.execute("SELECT handle, display_name FROM users WHERE id = ?", (user_id,)).fetchone()
    name = (user["display_name"] or user["handle"]) if user else "A manager"

    conn.execute("DELETE FROM picks WHERE league_id = ? AND user_id = ?", (league_id, user_id))
    conn.execute("DELETE FROM standings WHERE league_id = ? AND user_id = ?", (league_id, user_id))
    conn.execute(
        """
        UPDATE playoff_matchups
        SET user_a_id = CASE WHEN user_a_id = ? THEN NULL ELSE user_a_id END,
            user_b_id = CASE WHEN user_b_id = ? THEN NULL ELSE user_b_id END,
            winner_user_id = CASE WHEN winner_user_id = ? THEN NULL ELSE winner_user_id END,
            updated_at = ?
        WHERE league_id = ? AND (user_a_id = ? OR user_b_id = ? OR winner_user_id = ?)
        """,
        (user_id, user_id, user_id, now_iso(), league_id, user_id, user_id, user_id),
    )
    conn.execute("DELETE FROM activity_events WHERE league_id = ? AND user_id = ?", (league_id, user_id))
    conn.execute("DELETE FROM memberships WHERE league_id = ? AND user_id = ?", (league_id, user_id))
    log_activity(conn, league_id, None, "member_left", f"{name} left the league")
    return {"left": league_id, "name": league["name"]}


def validate_league_settings(settings: dict) -> None:
    if int(settings["min_stake"]) > int(settings["max_stake"]):
        raise ApiError(400, "Minimum stake cannot exceed maximum stake")
    playoff_size = int(settings["playoff_size"])
    if playoff_size not in PLAYOFF_SIZES:
        raise ApiError(400, "Playoff size must be 4, 6, 8, 10, 12, 14, or 16")
    if playoff_size > 32:
        raise ApiError(400, "Playoff leagues must have 32 or fewer managers")
    if int(settings["season_weeks"]) < 1 or int(settings["season_weeks"]) > 26:
        raise ApiError(400, "Season length must be between 1 and 26 weeks")
    if int(settings["playoff_weeks"]) < 1 or int(settings["playoff_weeks"]) > 4:
        raise ApiError(400, "Playoff duration must be between 1 and 4 weeks")


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ensure_active_slate(conn: sqlite3.Connection, league_id: str) -> dict:
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if not league:
        raise ApiError(404, "League not found")
    slate = conn.execute(
        "SELECT * FROM slates WHERE league_id = ? AND status = 'open' ORDER BY week DESC LIMIT 1",
        (league_id,),
    ).fetchone()
    if slate:
        refresh_stale_slate_schedule(conn, slate, int(league["matchups_per_slate"]))
        slate = conn.execute("SELECT * FROM slates WHERE id = ?", (slate["id"],)).fetchone()
        hydrate_slate_games(conn, slate["id"])
        sync_slate_stats(conn, slate["id"])
        slate = conn.execute("SELECT * FROM slates WHERE id = ?", (slate["id"],)).fetchone()
        if slate["status"] != "open":
            return ensure_active_slate(conn, league_id)
        return slate_dict(conn, slate)
    latest = conn.execute("SELECT COALESCE(MAX(week), 0) week FROM slates WHERE league_id = ?", (league_id,)).fetchone()
    week = int(latest["week"]) + 1
    for _ in range(10):
        slate_id = new_id("slt")
        locks_at = (datetime.now(timezone.utc) + timedelta(hours=38)).isoformat()
        result = conn.execute(
            """
            INSERT OR IGNORE INTO slates (id, league_id, week, status, game_date, locks_at, created_at)
            VALUES (?, ?, ?, 'open', ?, ?, ?)
            """,
            (slate_id, league_id, week, datetime.now().astimezone().date().isoformat(), locks_at, now_iso()),
        )
        if result.rowcount == 0:
            slate = conn.execute(
                "SELECT * FROM slates WHERE league_id = ? AND status = 'open' ORDER BY week DESC LIMIT 1",
                (league_id,),
            ).fetchone()
            if slate:
                return slate_dict(conn, slate)
            week += 1
            continue
        meta = generate_matchups(conn, slate_id, int(league["matchups_per_slate"]))
        conn.execute(
            "UPDATE slates SET game_date = ?, locks_at = ? WHERE id = ?",
            (meta["game_date"], meta["locks_at"], slate_id),
        )
        sync_slate_stats(conn, slate_id)
        return slate_dict(conn, conn.execute("SELECT * FROM slates WHERE id = ?", (slate_id,)).fetchone())
    raise ApiError(500, "Could not create a new slate")


def refresh_active_slate(conn: sqlite3.Connection, user_id: str, league_id: str) -> dict:
    membership = require_member(conn, user_id, league_id)
    if membership["role"] != "owner":
        raise ApiError(403, "Only the league owner can refresh the slate")
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    if not league:
        raise ApiError(404, "League not found")
    slate = conn.execute(
        "SELECT * FROM slates WHERE league_id = ? AND status = 'open' ORDER BY week DESC LIMIT 1",
        (league_id,),
    ).fetchone()
    if not slate:
        return ensure_active_slate(conn, league_id)
    conn.execute("DELETE FROM picks WHERE slate_id = ?", (slate["id"],))
    conn.execute("DELETE FROM matchups WHERE slate_id = ?", (slate["id"],))
    meta = generate_matchups(conn, slate["id"], int(league["matchups_per_slate"]), force_schedule_refresh=True)
    matchup_count = conn.execute("SELECT COUNT(*) c FROM matchups WHERE slate_id = ?", (slate["id"],)).fetchone()["c"]
    if matchup_count == 0:
        raise ApiError(503, "MLB schedule did not return eligible matchups; kept the previous slate. Try again in a minute.")
    conn.execute(
        "UPDATE slates SET game_date = ?, locks_at = ? WHERE id = ?",
        (meta["game_date"], meta["locks_at"], slate["id"]),
    )
    _SLATE_SYNC_CACHE.pop(slate["id"], None)
    sync_slate_stats(conn, slate["id"])
    return slate_dict(conn, conn.execute("SELECT * FROM slates WHERE id = ?", (slate["id"],)).fetchone())


def generate_matchups(conn: sqlite3.Connection, slate_id: str, count: int, force_schedule_refresh: bool = False) -> dict:
    players = conn.execute("SELECT * FROM players WHERE active = 1").fetchall()
    games = load_game_schedule(players, force_refresh=force_schedule_refresh)
    sync_probable_pitchers(conn, games)
    sync_lineup_batters(conn, games)
    players = conn.execute("SELECT * FROM players WHERE active = 1").fetchall()
    eligibility = build_daily_eligibility(players, games)
    eligible_players = [item.player for item in eligibility]
    eligibility_by_player = {item.player["id"]: item for item in eligibility}

    groups: dict[str, list[sqlite3.Row]] = {}
    for player in eligible_players:
        for stat_group in player_matchup_groups(player):
            groups.setdefault(stat_group, []).append(player)
    rows = []
    for stat_group, group_players in groups.items():
        if stat_group in PITCHER_STAT_GROUPS:
            # Real MLB mode must never pad pitcher matchups with static/non-probable starters.
            group_players = [
                p
                for p in group_players
                if eligibility_by_player[p["id"]].role == "probable_starting_pitcher"
            ]
        random.shuffle(group_players)
        for idx in range(0, len(group_players) - 1, 2):
            a, b = group_players[idx], group_players[idx + 1]
            elig_a = eligibility_by_player[a["id"]]
            elig_b = eligibility_by_player[b["id"]]
            game_a = elig_a.game
            game_b = elig_b.game
            game = min((game_a, game_b), key=lambda g: g.start_time or "")
            rule = STAT_RULES[stat_group]
            rows.append(
                {
                    "id": new_id("mat"),
                    "stat_group": stat_group,
                    "probable_starter": stat_group in PITCHER_STAT_GROUPS and str(a["id"]).startswith("mlb_") and str(b["id"]).startswith("mlb_"),
                    "label": rule["label"],
                    "unit": rule["unit"],
                    "a": a,
                    "b": b,
                    "elig_a": elig_a,
                    "elig_b": elig_b,
                    "game": game,
                    "game_a": game_a,
                    "game_b": game_b,
                    "line": matchup_line(stat_group),
                }
            )
    starter_rows = [r for r in rows if r["probable_starter"]]
    other_rows = [r for r in rows if not r["probable_starter"]]
    starter_priority = {key: idx for idx, key in enumerate(PITCHER_STAT_GROUPS)}
    starter_rows.sort(key=lambda r: (r["game"].start_time or "", starter_priority.get(r["stat_group"], 99)))
    other_rows.sort(key=lambda r: (r["game"].start_time or "", r["stat_group"]))
    starter_stat_count = len({r["stat_group"] for r in starter_rows})
    starter_target = min(len(starter_rows), max(1, starter_stat_count, count // 3)) if starter_rows else 0
    rows = diverse_matchup_rows(starter_rows, other_rows, starter_target, count)
    rows.sort(key=lambda r: (r["game"].start_time or "", 0 if r["probable_starter"] else 1, r["stat_group"]))
    for row in rows:
        game = row["game"]
        last5_a = recent_player_values(row["a"]["external_id"], row["stat_group"])
        last5_b = recent_player_values(row["b"]["external_id"], row["stat_group"])
        market = seeded_market_points(row["stat_group"], last5_a, last5_b)
        log_slate_inclusion(row["a"], row["elig_a"])
        log_slate_inclusion(row["b"], row["elig_b"])
        conn.execute(
            """
            INSERT INTO matchups (
              id, slate_id, stat_key, stat_label, unit, player_a_id, player_b_id,
              game_pk, game_pk_a, game_pk_b, game_start_a, game_start_b, game_start,
              game_status_a, game_status_b, inning_a, inning_b, live_state_a, live_state_b,
              opponent_a, opponent_b, eligibility_role_a, eligibility_role_b,
              eligibility_reason_a, eligibility_reason_b, eligibility_confidence_a, eligibility_confidence_b,
              game_status, inning, live_state, stat_current_a, stat_current_b, last5_a, last5_b,
              projection_line, pub_a, pub_b, pub_tie
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                slate_id,
                row["stat_group"],
                row["label"],
                row["unit"],
                row["a"]["id"],
                row["b"]["id"],
                game.game_pk,
                row["game_a"].game_pk,
                row["game_b"].game_pk,
                row["game_a"].start_time,
                row["game_b"].start_time,
                game.start_time,
                row["game_a"].status,
                row["game_b"].status,
                row["game_a"].inning,
                row["game_b"].inning,
                row["game_a"].live_state,
                row["game_b"].live_state,
                row["elig_a"].opponent,
                row["elig_b"].opponent,
                row["elig_a"].role,
                row["elig_b"].role,
                row["elig_a"].reason,
                row["elig_b"].reason,
                row["elig_a"].confidence,
                row["elig_b"].confidence,
                game.status,
                game.inning,
                game.live_state,
                0,
                0,
                json.dumps(last5_a),
                json.dumps(last5_b),
                row["line"],
                market["a"],
                market["b"],
                market["tie"],
            ),
        )
    starts = [r["game"].start_time for r in rows if r["game"].start_time]
    dates = [r["game"].game_date for r in rows if r["game"].game_date]
    return {
        "locks_at": min(starts) if starts else (datetime.now(timezone.utc) + timedelta(hours=38)).isoformat(),
        "game_date": min(dates) if dates else datetime.now().astimezone().date().isoformat(),
    }


def build_daily_eligibility(players: list[sqlite3.Row], games: list) -> list[PlayerEligibility]:
    game_by_team: dict[str, Any] = {}
    opponent_by_team: dict[str, str] = {}
    lineups_by_team: dict[str, set[str]] = {}
    for game in games:
        away, home = (game.teams + ("", ""))[:2]
        if away:
            game_by_team.setdefault(away, game)
            opponent_by_team.setdefault(away, home)
        if home:
            game_by_team.setdefault(home, game)
            opponent_by_team.setdefault(home, away)
        feed = cached_live_feed(game.game_pk)
        for team, lineup in ((feed or {}).get("lineups") or {}).items():
            lineups_by_team[team] = {f"mlb_{str(player['id'])}" for player in lineup}

    static_mode = bool(games) and all(str(game.game_pk).startswith("static-") for game in games)
    probable_ids = {
        str(pitcher.id)
        for game in games
        for pitcher in (getattr(game, "probable_pitchers", ()) or ())
    }
    eligible: list[PlayerEligibility] = []
    for player in players:
        game = game_by_team.get(player["team"])
        if not game:
            continue

        if player["stat_group"] == "K":
            if static_mode:
                eligible.append(
                    PlayerEligibility(player, game, opponent_by_team.get(player["team"], ""), "demo_starting_pitcher", "demo_mode_static_schedule", "demo")
                )
            elif str(player["id"]) in probable_ids:
                eligible.append(
                    PlayerEligibility(player, game, opponent_by_team.get(player["team"], ""), "probable_starting_pitcher", "mlb_probable_pitcher", "confirmed")
                )
            continue

        lineup_ids = lineups_by_team.get(player["team"])
        if lineup_ids is not None:
            if player["id"] not in lineup_ids:
                continue
            confidence = "confirmed"
            reason = "confirmed_starting_lineup"
        else:
            confidence = "demo" if static_mode else "team_scheduled"
            reason = "demo_mode_static_schedule" if static_mode else "team_on_today_schedule"
        eligible.append(PlayerEligibility(player, game, opponent_by_team.get(player["team"], ""), "batter", reason, confidence))

    return eligible


def log_slate_inclusion(player: sqlite3.Row, eligibility: PlayerEligibility) -> None:
    LOGGER.info(
        "slate eligibility included player=%s external_id=%s stat=%s team=%s game_pk=%s reason=%s confidence=%s",
        player["name"],
        player["external_id"],
        player["stat_group"],
        player["team"],
        eligibility.game.game_pk,
        eligibility.reason,
        eligibility.confidence,
    )


def sync_probable_pitchers(conn: sqlite3.Connection, games: list) -> None:
    for game in games:
        for pitcher in getattr(game, "probable_pitchers", ()) or ():
            conn.execute(
                """
                INSERT INTO players (id, external_id, name, team, position, stat_group, active, updated_at)
                VALUES (?, ?, ?, ?, ?, 'K', 1, ?)
                ON CONFLICT(id) DO UPDATE SET
                  external_id = excluded.external_id,
                  name = excluded.name,
                  team = excluded.team,
                  position = excluded.position,
                  stat_group = 'K',
                  active = 1,
                  updated_at = excluded.updated_at
                """,
                (pitcher.id, pitcher.external_id, pitcher.name, pitcher.team, pitcher.position, now_iso()),
            )


def sync_lineup_batters(conn: sqlite3.Connection, games: list) -> None:
    for game in games:
        feed = cached_live_feed(game.game_pk)
        for lineup in ((feed or {}).get("lineups") or {}).values():
            for batter in lineup:
                player_id = str(batter["id"])
                stat_group = lineup_stat_group(player_id, batter.get("batting_order"))
                conn.execute(
                    """
                    INSERT INTO players (id, external_id, name, team, position, stat_group, active, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      external_id = excluded.external_id,
                      name = excluded.name,
                      team = excluded.team,
                      position = excluded.position,
                      active = 1,
                      updated_at = excluded.updated_at
                    """,
                    (
                        f"mlb_{player_id}",
                        player_id,
                        batter["name"],
                        batter["team"],
                        batter["position"],
                        stat_group,
                        now_iso(),
                    ),
                )


def lineup_stat_group(player_id: str, batting_order: int | None = None) -> str:
    if batting_order:
        return BATTER_STAT_GROUPS[((int(batting_order) // 100) - 1) % len(BATTER_STAT_GROUPS)]
    seed = sum(int(ch) for ch in player_id if ch.isdigit())
    return BATTER_STAT_GROUPS[seed % len(BATTER_STAT_GROUPS)]


def player_matchup_groups(player: sqlite3.Row) -> list[str]:
    primary = player["stat_group"]
    if primary in PITCHER_STAT_GROUPS:
        return list(PITCHER_STAT_GROUPS)
    seed = sum(int(ch) for ch in str(player["external_id"] or player["id"]) if ch.isdigit())
    groups = [primary if primary in BATTER_STAT_GROUPS else BATTER_STAT_GROUPS[seed % len(BATTER_STAT_GROUPS)]]
    groups.append(BATTER_STAT_GROUPS[(seed + 3) % len(BATTER_STAT_GROUPS)])
    groups.append(BATTER_STAT_GROUPS[(seed + 6) % len(BATTER_STAT_GROUPS)])
    return list(dict.fromkeys(groups))


def diverse_matchup_rows(starter_rows: list[dict], other_rows: list[dict], starter_target: int, count: int) -> list[dict]:
    selected: list[dict] = []
    selected_ids: set[str] = set()
    for stat_group in PITCHER_STAT_GROUPS:
        row = next((r for r in starter_rows if r["stat_group"] == stat_group and r["id"] not in selected_ids), None)
        if row and len(selected) < starter_target:
            selected.append(row)
            selected_ids.add(row["id"])
    for row in starter_rows:
        if len(selected) >= starter_target:
            break
        if row["id"] not in selected_ids:
            selected.append(row)
            selected_ids.add(row["id"])
    usage: dict[str, int] = {}
    for row in selected:
        usage[row["a"]["id"]] = usage.get(row["a"]["id"], 0) + 1
        usage[row["b"]["id"]] = usage.get(row["b"]["id"], 0) + 1

    for max_usage in (1, 2, 3):
        for row in other_rows:
            if len(selected) >= count:
                return selected
            if row["id"] in selected_ids:
                continue
            a_id = row["a"]["id"]
            b_id = row["b"]["id"]
            if usage.get(a_id, 0) >= max_usage or usage.get(b_id, 0) >= max_usage:
                continue
            selected.append(row)
            selected_ids.add(row["id"])
            usage[a_id] = usage.get(a_id, 0) + 1
            usage[b_id] = usage.get(b_id, 0) + 1
    return selected[:count]


def recent_player_values(external_id: str, stat_key: str) -> list[float]:
    if not external_id:
        return []
    group = "pitching" if stat_key in PITCHER_STAT_GROUPS else "hitting"
    try:
        splits = cached_game_log(str(external_id), group)
    except RuntimeError:
        return []
    values: list[float] = []
    for split in splits:
        stat = split.get("stat") or {}
        if stat_key in PITCHER_STAT_GROUPS and int(stat.get("gamesStarted") or 0) <= 0:
            continue
        values.append(player_stat_value(stat_key, stat))
        if len(values) >= 5:
            break
    return values


def weighted_recent_projection(values: list[float]) -> float | None:
    clean = [float(v) for v in values[:5] if isinstance(v, (int, float)) and v >= 0]
    if not clean:
        return None
    weights = list(range(len(clean) + 1, 1, -1))
    return sum(v * w for v, w in zip(clean, weights)) / sum(weights)


def seeded_market_points(stat_key: str, last5_a: list[float], last5_b: list[float]) -> dict[str, int]:
    """Seed fake/public money from recent form with enough noise to feel human.

    The side with the better weighted last-5 projection should usually be chalk,
    but close projections increase tie interest and random market drift can still
    leave a better payout on the projected winner.
    """
    proj_a = weighted_recent_projection(last5_a)
    proj_b = weighted_recent_projection(last5_b)
    line_low, line_high = STAT_RULES.get(stat_key, {"line": (2, 6)})["line"]
    fallback = max(0.2, (float(line_low) + float(line_high)) / 4)
    if proj_a is None and proj_b is None:
        proj_a = fallback * random.uniform(0.85, 1.15)
        proj_b = fallback * random.uniform(0.85, 1.15)
    elif proj_a is None:
        proj_a = max(0.0, float(proj_b) * random.uniform(0.82, 1.08))
    elif proj_b is None:
        proj_b = max(0.0, float(proj_a) * random.uniform(0.82, 1.08))

    assert proj_a is not None and proj_b is not None
    avg = max(0.45, (abs(proj_a) + abs(proj_b)) / 2)
    edge = proj_a - proj_b
    closeness = max(0.0, 1.0 - abs(edge) / max(avg, 1.0))
    edge_strength = min(1.0, abs(edge) / max(avg, 1.0))

    favorite_share = 0.53 + edge_strength * 0.28 + random.uniform(-0.025, 0.025)
    favorite_share = max(0.50, min(0.82, favorite_share))
    tie_share = 0.04 + closeness * 0.10 + random.uniform(-0.012, 0.012)
    if stat_key in {"HR", "BB", "XBH", "H", "R", "RBI"}:
        tie_share += 0.018
    tie_share = max(0.035, min(0.18, tie_share))

    side_pool = max(0.70, 1.0 - tie_share)
    if abs(edge) < avg * 0.04:
        a_share = side_pool * (0.5 + random.uniform(-0.025, 0.025))
    elif edge > 0:
        a_share = side_pool * favorite_share
    else:
        a_share = side_pool * (1.0 - favorite_share)
    b_share = side_pool - a_share

    total = random.randint(950, 1700)
    a = max(90, round(total * a_share))
    b = max(90, round(total * b_share))
    tie = max(35, round(total * tie_share))
    return {"a": a, "b": b, "tie": tie}


def cached_game_log(external_id: str, group: str) -> list[dict]:
    season = datetime.now().astimezone().year
    key = (external_id, group, season)
    now = datetime.now(timezone.utc)
    cached = _GAME_LOG_CACHE.get(key)
    if cached and (now - cached[0]).total_seconds() < 900:
        return cached[1]
    splits = MlbGameLogProvider(timeout=4.0).game_log(external_id, group, season)
    _GAME_LOG_CACHE[key] = (now, splits)
    return splits


def player_stat_value(stat_key: str, stat: dict) -> float:
    if stat_key == "K":
        return float(stat.get("strikeOuts") or 0)
    if stat_key == "BF":
        return float(stat.get("battersFaced") or 0)
    if stat_key == "IP":
        return float(stat.get("inningsPitched") or 0)
    if stat_key == "TB":
        return float(stat.get("totalBases") or 0)
    if stat_key == "OB":
        return float((stat.get("hits") or 0) + (stat.get("baseOnBalls") or 0))
    if stat_key == "HR":
        return float(stat.get("homeRuns") or 0)
    if stat_key == "SPD":
        return float((stat.get("runs") or 0) + (stat.get("stolenBases") or 0))
    if stat_key == "H":
        return float(stat.get("hits") or 0)
    if stat_key == "R":
        return float(stat.get("runs") or 0)
    if stat_key == "RBI":
        return float(stat.get("rbi") or 0)
    if stat_key == "XBH":
        return float((stat.get("doubles") or 0) + (stat.get("triples") or 0) + (stat.get("homeRuns") or 0))
    if stat_key == "HHR":
        return float((stat.get("hits") or 0) + (stat.get("runs") or 0) + (stat.get("rbi") or 0))
    if stat_key == "BB":
        return float(stat.get("baseOnBalls") or 0)
    return 0.0


def refresh_stale_slate_schedule(conn: sqlite3.Connection, slate: sqlite3.Row, count: int) -> None:
    picks = conn.execute("SELECT COUNT(*) c FROM picks WHERE slate_id = ?", (slate["id"],)).fetchone()["c"]
    if picks:
        return
    today = datetime.now().astimezone().date().isoformat()
    stale = conn.execute(
        """
        SELECT COUNT(*) c FROM matchups
        WHERE slate_id = ? AND (game_pk IS NULL OR game_pk LIKE 'static-%')
        """,
        (slate["id"],),
    ).fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM matchups WHERE slate_id = ?", (slate["id"],)).fetchone()["c"]
    starter_k = conn.execute(
        """
        SELECT COUNT(*) c
        FROM matchups
        WHERE slate_id = ? AND stat_key = 'K' AND player_a_id LIKE 'mlb_%' AND player_b_id LIKE 'mlb_%'
        """,
        (slate["id"],),
    ).fetchone()["c"]
    should_refresh = (not total) or stale >= total or (slate["game_date"] and slate["game_date"] != today) or starter_k == 0
    if not should_refresh:
        return
    conn.execute("DELETE FROM matchups WHERE slate_id = ?", (slate["id"],))
    meta = generate_matchups(conn, slate["id"], count)
    conn.execute("UPDATE slates SET game_date = ?, locks_at = ? WHERE id = ?", (meta["game_date"], meta["locks_at"], slate["id"]))


def load_game_schedule(players: list[sqlite3.Row], force_refresh: bool = False):
    today = datetime.now().astimezone().date()
    now = datetime.now(timezone.utc)
    if os.environ.get("CHALKED_DISABLE_MLB") == "1":
        LOGGER.warning("CHALKED_DISABLE_MLB=1; using demo static schedule")
        static_players = [
            type("PlayerShim", (), {"team": p["team"]})()
            for p in players
        ]
        return static_schedule(static_players, today)
    try:
        games = cached_mlb_schedule(today, force_refresh=force_refresh)
        playable = [
            g
            for g in games
            if g.start_time
            and (
                parse_utc(g.start_time) > now
                or (g.live_state or "").lower() == "live"
                or "progress" in (g.status or "").lower()
            )
        ]
        if playable:
            return playable
        for offset in range(1, 4):
            upcoming = cached_mlb_schedule(today + timedelta(days=offset), force_refresh=force_refresh)
            future_games = [g for g in upcoming if g.start_time and parse_utc(g.start_time) > now]
            if future_games:
                return future_games
        return []
    except RuntimeError as exc:
        LOGGER.warning("MLB schedule unavailable; refusing static fallback in real mode: %s", exc)
        return []


def cached_mlb_schedule(target_date, force_refresh: bool = False) -> list:
    key = target_date.isoformat()
    now = datetime.now(timezone.utc)
    cached = _SCHEDULE_CACHE.get(key)
    if not force_refresh and cached and (now - cached[0]).total_seconds() < 180:
        return cached[1]
    games = MlbScheduleProvider().schedule(target_date)
    _SCHEDULE_CACHE[key] = (now, games)
    return games


def cached_live_feed(game_pk: str) -> dict | None:
    now = datetime.now(timezone.utc)
    cached = _LIVE_FEED_CACHE.get(game_pk)
    if cached and (now - cached[0]).total_seconds() < 20:
        return cached[1]
    try:
        feed = MlbLiveFeedProvider(timeout=2.0).game(game_pk)
    except RuntimeError:
        feed = None
    _LIVE_FEED_CACHE[game_pk] = (now, feed)
    return feed


def hydrate_slate_games(conn: sqlite3.Connection, slate_id: str) -> None:
    stale = conn.execute(
        """
        SELECT COUNT(*) c FROM matchups
        WHERE slate_id = ? AND (game_start IS NULL OR game_pk IS NULL OR game_pk LIKE 'static-%')
        """,
        (slate_id,),
    ).fetchone()["c"]
    if not stale:
        meta = conn.execute("SELECT MIN(game_start) locks_at FROM matchups WHERE slate_id = ? AND game_start IS NOT NULL", (slate_id,)).fetchone()
        if meta and meta["locks_at"]:
            conn.execute("UPDATE slates SET locks_at = ? WHERE id = ?", (meta["locks_at"], slate_id))
        return
    rows = conn.execute(
        """
        SELECT m.id, pa.team team_a, pb.team team_b
        FROM matchups m
        JOIN players pa ON pa.id = m.player_a_id
        JOIN players pb ON pb.id = m.player_b_id
        WHERE m.slate_id = ?
        ORDER BY m.id
        """,
        (slate_id,),
    ).fetchall()
    players = conn.execute("SELECT * FROM players WHERE active = 1").fetchall()
    games = load_game_schedule(players)
    if not games:
        return
    sync_probable_pitchers(conn, games)
    game_by_team = {}
    for game in games:
        for team in game.teams:
            if team and team not in game_by_team:
                game_by_team[team] = game
    starts = []
    dates = []
    for idx, row in enumerate(rows):
        game_a = game_by_team.get(row["team_a"]) or games[idx % len(games)]
        game_b = game_by_team.get(row["team_b"]) or games[(idx + 1) % len(games)]
        game = min((game_a, game_b), key=lambda g: g.start_time or "")
        starts.append(game.start_time)
        dates.append(game.game_date)
        conn.execute(
            """
            UPDATE matchups
            SET game_pk = ?, game_pk_a = ?, game_pk_b = ?, game_start_a = ?, game_start_b = ?,
                game_status_a = ?, game_status_b = ?, inning_a = ?, inning_b = ?, live_state_a = ?, live_state_b = ?,
                game_start = ?, game_status = ?, inning = ?, live_state = ?,
                stat_current_a = COALESCE(stat_current_a, 0), stat_current_b = COALESCE(stat_current_b, 0)
            WHERE id = ?
            """,
            (
                game.game_pk,
                game_a.game_pk,
                game_b.game_pk,
                game_a.start_time,
                game_b.start_time,
                game_a.status,
                game_b.status,
                game_a.inning,
                game_b.inning,
                game_a.live_state,
                game_b.live_state,
                game.start_time,
                game.status,
                game.inning,
                game.live_state,
                row["id"],
            ),
        )
    if starts:
        conn.execute("UPDATE slates SET game_date = ?, locks_at = ? WHERE id = ?", (min(dates), min(starts), slate_id))


def slate_dict(conn: sqlite3.Connection, slate: sqlite3.Row) -> dict:
    matchups = conn.execute(
        """
        SELECT m.*, pa.name player_a_name, pa.team player_a_team, pa.position player_a_position, pa.external_id player_a_external_id,
               pb.name player_b_name, pb.team player_b_team, pb.position player_b_position, pb.external_id player_b_external_id
        FROM matchups m
        JOIN players pa ON pa.id = m.player_a_id
        JOIN players pb ON pb.id = m.player_b_id
        WHERE m.slate_id = ?
        ORDER BY COALESCE(m.game_start, ''), m.id
        """,
        (slate["id"],),
    ).fetchall()
    return {
        "id": slate["id"],
        "league_id": slate["league_id"],
        "week": slate["week"],
        "day": ((slate["week"] - 1) % 7) + 1,
        "status": slate["status"],
        "game_date": slate["game_date"],
        "locks_at": slate["locks_at"],
        "matchups": [matchup_dict(conn, m) for m in matchups],
    }


def recent_slates(conn: sqlite3.Connection, user_id: str, league_id: str, limit: int = 4) -> dict:
    require_member(conn, user_id, league_id)
    ensure_active_slate(conn, league_id)
    rows = conn.execute(
        """
        SELECT * FROM slates
        WHERE league_id = ?
        ORDER BY week DESC, created_at DESC
        LIMIT ?
        """,
        (league_id, limit),
    ).fetchall()
    slates = []
    for row in rows:
        if row["status"] == "open":
            hydrate_slate_games(conn, row["id"])
            sync_slate_stats(conn, row["id"])
            row = conn.execute("SELECT * FROM slates WHERE id = ?", (row["id"],)).fetchone()
        item = slate_dict(conn, row)
        picks = conn.execute(
            "SELECT * FROM picks WHERE user_id = ? AND league_id = ? AND slate_id = ? ORDER BY created_at",
            (user_id, league_id, row["id"]),
        ).fetchall()
        item["picks"] = [row_to_dict(p) for p in picks]
        slates.append(item)
    active = next((s for s in slates if s["status"] == "open"), slates[0] if slates else None)
    return {"active_slate_id": active["id"] if active else None, "slates": slates}


def matchup_dict(conn: sqlite3.Connection, m: sqlite3.Row) -> dict:
    totals = side_totals(conn, m["id"])
    now = datetime.now(timezone.utc)
    game_a_locked = (m["live_state_a"] or m["live_state"] or "").lower() in ("live", "final") or bool((m["game_start_a"] or m["game_start"]) and now >= parse_utc(m["game_start_a"] or m["game_start"]))
    game_b_locked = (m["live_state_b"] or m["live_state"] or "").lower() in ("live", "final") or bool((m["game_start_b"] or m["game_start"]) and now >= parse_utc(m["game_start_b"] or m["game_start"]))
    game_a = {
        "game_pk": m["game_pk_a"] or m["game_pk"],
        "start": m["game_start_a"] or m["game_start"],
        "status": m["game_status_a"] or m["game_status"] or "Scheduled",
        "inning": m["inning_a"],
        "live_state": m["live_state_a"] or m["live_state"] or "Preview",
        "is_live": (m["live_state_a"] or m["live_state"] or "").lower() == "live",
        "locks_at": m["game_start_a"] or m["game_start"],
        "is_locked": game_a_locked,
    }
    game_b = {
        "game_pk": m["game_pk_b"] or m["game_pk"],
        "start": m["game_start_b"] or m["game_start"],
        "status": m["game_status_b"] or m["game_status"] or "Scheduled",
        "inning": m["inning_b"],
        "live_state": m["live_state_b"] or m["live_state"] or "Preview",
        "is_live": (m["live_state_b"] or m["live_state"] or "").lower() == "live",
        "locks_at": m["game_start_b"] or m["game_start"],
        "is_locked": game_b_locked,
    }
    return {
        "id": m["id"],
        "stat_key": m["stat_key"],
        "stat_label": STAT_RULES.get(m["stat_key"], {}).get("label", m["stat_label"]).replace("This Week", "Game"),
        "unit": m["unit"],
        "projection_line": m["projection_line"],
        "players": {
            "a": {
                "id": m["player_a_id"],
                "external_id": m["player_a_external_id"],
                "name": m["player_a_name"],
                "team": m["player_a_team"],
                "position": m["player_a_position"],
                "opponent": m["opponent_a"],
                "eligibility": {
                    "role": m["eligibility_role_a"],
                    "reason": m["eligibility_reason_a"],
                    "confidence": m["eligibility_confidence_a"],
                },
                "game": game_a,
            },
            "b": {
                "id": m["player_b_id"],
                "external_id": m["player_b_external_id"],
                "name": m["player_b_name"],
                "team": m["player_b_team"],
                "position": m["player_b_position"],
                "opponent": m["opponent_b"],
                "eligibility": {
                    "role": m["eligibility_role_b"],
                    "reason": m["eligibility_reason_b"],
                    "confidence": m["eligibility_confidence_b"],
                },
                "game": game_b,
            },
        },
        "market": totals,
        "game": {
            "game_pk": m["game_pk"],
            "start": m["game_start"],
            "status": m["game_status"] or "Scheduled",
            "inning": m["inning"],
            "live_state": m["live_state"] or "Preview",
            "is_live": (m["live_state"] or "").lower() == "live",
            "locks_at": m["game_start"],
            "is_locked": matchup_locked(m),
        },
        "live_stats": {"a": m["stat_current_a"] or 0, "b": m["stat_current_b"] or 0},
        "last5": {"a": parse_last5(m["last5_a"]), "b": parse_last5(m["last5_b"])},
        "audit": {
            "source": m["stat_source"] or "MLB StatsAPI",
            "stat_synced_at": m["stat_synced_at"],
            "settled_at": m["settled_at"],
        },
        "status": m["status"],
        "winner_side": m["winner_side"],
        "actual_a": m["actual_a"],
        "actual_b": m["actual_b"],
        "margin_bonus_hit": bool(m["margin_bonus_hit"]),
        "chat": matchup_chat_messages(conn, m["id"]),
    }


def public_matchup_share(conn: sqlite3.Connection, matchup_id: str, pick_id: str | None = None) -> dict:
    row = conn.execute(
        """
        SELECT m.*, s.week, s.game_date, s.status slate_status,
               l.id league_id, l.name league_name, l.min_mult, l.max_mult,
               pa.name player_a_name, pa.team player_a_team, pa.position player_a_position, pa.external_id player_a_external_id,
               pb.name player_b_name, pb.team player_b_team, pb.position player_b_position, pb.external_id player_b_external_id
        FROM matchups m
        JOIN slates s ON s.id = m.slate_id
        JOIN leagues l ON l.id = s.league_id
        JOIN players pa ON pa.id = m.player_a_id
        JOIN players pb ON pb.id = m.player_b_id
        WHERE m.id = ?
        """,
        (matchup_id,),
    ).fetchone()
    if not row:
        raise ApiError(404, "Matchup not found")

    stat_label = STAT_RULES.get(row["stat_key"], {}).get("label", row["stat_label"]).replace("This Week", "Game")
    week = int(row["week"] or 1)
    day = ((week - 1) % 7) + 1
    game_start = row["game_start"] or row["game_start_a"] or row["game_start_b"]
    start_label = ""
    if game_start:
        try:
            start_label = parse_utc(game_start).strftime("%b %d, %I:%M %p UTC").replace(" 0", " ")
        except ValueError:
            start_label = str(game_start)
    market_total = int(row["pub_a"] or 0) + int(row["pub_b"] or 0) + int(row["pub_tie"] or 0)
    title = f"{row['player_a_name']} vs {row['player_b_name']} - {stat_label}"
    context = f"Week {((week - 1) // 7) + 1}, Day {day}"
    parts = [
        f"{row['league_name']} matchup",
        context,
        start_label,
        f"{market_total:,} crowd pts" if market_total else "",
    ]
    description = " - ".join(part for part in parts if part)
    description = f"{description}. Pick the player, call the tie, or fade the crowd."
    pick = None
    if pick_id:
        pick_row = conn.execute(
            """
            SELECT p.*, u.handle, u.display_name
            FROM picks p
            JOIN users u ON u.id = p.user_id
            WHERE p.id = ? AND p.matchup_id = ?
            """,
            (pick_id, row["id"]),
        ).fetchone()
        if pick_row:
            pick = row_to_dict(pick_row)
            picked_side = pick["side"]
            pick["side_label"] = share_side_name(row, picked_side)
            pick["won"] = bool(row["winner_side"] and picked_side == row["winner_side"])
            if pick["status"] == "settled":
                if pick["won"]:
                    pick["result_label"] = f"won {int(pick['payout'] or 0):,} pts"
                else:
                    pick["result_label"] = f"lost {int(pick['stake'] or 0):,} pts"
            else:
                pick["result_label"] = f"{int(pick['stake'] or 0):,} pts @ {float(pick['mult_at_lock'] or 0):.2f}x"
    market = {
        "a": int(row["pub_a"] or 0),
        "b": int(row["pub_b"] or 0),
        "tie": int(row["pub_tie"] or 0),
        "total": market_total,
    }
    mults = {
        "a": share_multiplier(row["min_mult"], row["max_mult"], market["a"], market_total),
        "b": share_multiplier(row["min_mult"], row["max_mult"], market["b"], market_total),
        "tie": share_multiplier(row["min_mult"], row["max_mult"], market["tie"], market_total),
    }
    cache_source = row["settled_at"] or row["stat_synced_at"] or row["game_start"] or row["id"]
    cache_key = re.sub(r"[^A-Za-z0-9]+", "", str(cache_source))[-24:] or row["id"]
    return {
        "id": row["id"],
        "league_id": row["league_id"],
        "league_name": row["league_name"],
        "slate_id": row["slate_id"],
        "title": title,
        "description": description,
        "stat_label": stat_label,
        "unit": row["unit"],
        "game_start": game_start,
        "game_status": row["game_status"] or "Scheduled",
        "live_state": row["live_state"] or "Preview",
        "inning": row["inning"],
        "status": row["status"],
        "winner_side": row["winner_side"],
        "actual_a": row["actual_a"],
        "actual_b": row["actual_b"],
        "live_stats": {"a": row["stat_current_a"] or 0, "b": row["stat_current_b"] or 0},
        "market": market,
        "multipliers": mults,
        "pick": pick,
        "cache_key": cache_key,
        "players": {
            "a": {
                "name": row["player_a_name"],
                "team": row["player_a_team"],
                "position": row["player_a_position"],
                "opponent": row["opponent_a"],
                "external_id": row["player_a_external_id"],
            },
            "b": {
                "name": row["player_b_name"],
                "team": row["player_b_team"],
                "position": row["player_b_position"],
                "opponent": row["opponent_b"],
                "external_id": row["player_b_external_id"],
            },
        },
    }


def share_multiplier(min_mult: float, max_mult: float, points: int, total: int) -> float:
    if total <= 0 or points <= 0:
        return float(max_mult)
    return round(max(float(min_mult), min(float(max_mult), 1 / (points / total))), 2)


def share_side_name(row: sqlite3.Row, side: str) -> str:
    if side == "a":
        return row["player_a_name"]
    if side == "b":
        return row["player_b_name"]
    return "the tie"


def parse_last5(raw: str | None) -> list[float]:
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    return [float(v) for v in values[:5] if isinstance(v, (int, float))]


def sync_slate_stats(conn: sqlite3.Connection, slate_id: str, force: bool = False) -> None:
    slate = conn.execute("SELECT * FROM slates WHERE id = ?", (slate_id,)).fetchone()
    if not slate or slate["status"] != "open":
        return
    now = datetime.now(timezone.utc)
    last_sync = _SLATE_SYNC_CACHE.get(slate_id)
    if not force and last_sync and (now - last_sync).total_seconds() < 15:
        return
    _SLATE_SYNC_CACHE[slate_id] = now
    rows = conn.execute(
        """
        SELECT m.*, pa.external_id player_a_external, pb.external_id player_b_external
        FROM matchups m
        JOIN players pa ON pa.id = m.player_a_id
        JOIN players pb ON pb.id = m.player_b_id
        WHERE m.slate_id = ?
        """,
        (slate_id,),
    ).fetchall()
    if not rows:
        return
    feeds: dict[str, dict | None] = {}
    for row in rows:
        candidates = [
            (row["game_pk_a"] or row["game_pk"], row["game_start_a"] or row["game_start"], row["live_state_a"] or row["live_state"]),
            (row["game_pk_b"] or row["game_pk"], row["game_start_b"] or row["game_start"], row["live_state_b"] or row["live_state"]),
        ]
        for game_pk, game_start, live_state_value in candidates:
            if not game_pk or game_pk in feeds:
                continue
            live_state = (live_state_value or "").lower()
            if game_start and parse_utc(game_start) > now and live_state in ("preview", "pre-game", "scheduled"):
                continue
            feeds[game_pk] = cached_live_feed(game_pk)

    for row in rows:
        feed_a = feeds.get(row["game_pk_a"] or row["game_pk"])
        feed_b = feeds.get(row["game_pk_b"] or row["game_pk"])
        if not feed_a and not feed_b:
            continue
        players_a = (feed_a or {}).get("players") or {}
        players_b = (feed_b or {}).get("players") or {}
        stat_a = player_stat(row["stat_key"], players_a.get(str(row["player_a_external"])) or {})
        stat_b = player_stat(row["stat_key"], players_b.get(str(row["player_b_external"])) or {})
        status_a = (feed_a or {}).get("status") or row["game_status_a"] or row["game_status"] or "Scheduled"
        status_b = (feed_b or {}).get("status") or row["game_status_b"] or row["game_status"] or "Scheduled"
        state_a = (feed_a or {}).get("live_state") or row["live_state_a"] or row["live_state"] or "Preview"
        state_b = (feed_b or {}).get("live_state") or row["live_state_b"] or row["live_state"] or "Preview"
        inning_a = (feed_a or {}).get("inning") or row["inning_a"]
        inning_b = (feed_b or {}).get("inning") or row["inning_b"]
        final_a = is_final_game(status_a, state_a)
        final_b = is_final_game(status_b, state_b)
        status = combined_matchup_status(status_a, state_a, status_b, state_b, row["game_status"])
        live_state = combined_matchup_live_state(status_a, state_a, status_b, state_b, row["live_state"])
        inning = inning_a if state_a.lower() == "live" else inning_b if state_b.lower() == "live" else (feed_a or feed_b or {}).get("inning")
        final = bool(feed_a and feed_b and final_a and final_b)
        synced_at = now_iso()
        winner = None
        if final:
            winner = "tie" if stat_a == stat_b else "a" if stat_a > stat_b else "b"
        conn.execute(
            """
            UPDATE matchups
            SET game_status = ?, inning = ?, live_state = ?,
                game_status_a = ?, game_status_b = ?, inning_a = ?, inning_b = ?, live_state_a = ?, live_state_b = ?,
                stat_current_a = ?, stat_current_b = ?,
                actual_a = CASE WHEN ? THEN ? ELSE actual_a END,
                actual_b = CASE WHEN ? THEN ? ELSE actual_b END,
                winner_side = CASE WHEN ? THEN ? ELSE winner_side END,
                margin_bonus_hit = CASE WHEN ? THEN ? ELSE margin_bonus_hit END,
                stat_source = ?,
                stat_synced_at = ?,
                settled_at = CASE WHEN ? THEN COALESCE(settled_at, ?) ELSE settled_at END,
                status = CASE WHEN ? THEN 'settled' ELSE status END
            WHERE id = ?
            """,
            (
                status,
                inning,
                live_state,
                status_a,
                status_b,
                inning_a,
                inning_b,
                state_a,
                state_b,
                stat_a,
                stat_b,
                1 if final else 0,
                stat_a,
                1 if final else 0,
                stat_b,
                1 if final else 0,
                winner,
                1 if final else 0,
                1 if max(stat_a, stat_b) >= float(row["projection_line"]) * 1.2 else 0,
                "MLB StatsAPI liveFeed",
                synced_at,
                1 if final else 0,
                synced_at,
                1 if final else 0,
                row["id"],
            ),
        )
        if final:
            settle_matchup_picks(conn, row["id"])
    open_matchups = conn.execute(
        "SELECT COUNT(*) c FROM matchups WHERE slate_id = ? AND status = 'open'",
        (slate_id,),
    ).fetchone()["c"]
    if open_matchups == 0:
        settle_slate_from_matchups(conn, slate_id)


def settle_due_slates(conn: sqlite3.Connection, force: bool = True) -> dict:
    rows = conn.execute(
        """
        SELECT s.*, l.matchups_per_slate
        FROM slates s
        JOIN leagues l ON l.id = s.league_id
        WHERE s.status = 'open'
        ORDER BY s.game_date, s.week
        """
    ).fetchall()
    checked = 0
    settled = 0
    for slate in rows:
        checked += 1
        hydrate_slate_games(conn, slate["id"])
        before = slate["status"]
        sync_slate_stats(conn, slate["id"], force=force)
        after = conn.execute("SELECT status FROM slates WHERE id = ?", (slate["id"],)).fetchone()
        if before == "open" and after and after["status"] == "settled":
            settled += 1
            ensure_active_slate(conn, slate["league_id"])
    return {"checked": checked, "settled": settled}


def player_stat(stat_key: str, stats: dict) -> float:
    batting = stats.get("batting") or {}
    pitching = stats.get("pitching") or {}
    if stat_key == "K":
        return float(pitching.get("strikeOuts") or 0)
    if stat_key == "BF":
        return float(pitching.get("battersFaced") or 0)
    if stat_key == "IP":
        return float(pitching.get("inningsPitched") or 0)
    if stat_key == "TB":
        return float(batting.get("totalBases") or 0)
    if stat_key == "OB":
        return float((batting.get("hits") or 0) + (batting.get("baseOnBalls") or 0))
    if stat_key == "HR":
        return float(batting.get("homeRuns") or 0)
    if stat_key == "SPD":
        return float((batting.get("runs") or 0) + (batting.get("stolenBases") or 0))
    if stat_key == "H":
        return float(batting.get("hits") or 0)
    if stat_key == "R":
        return float(batting.get("runs") or 0)
    if stat_key == "RBI":
        return float(batting.get("rbi") or 0)
    if stat_key == "XBH":
        return float((batting.get("doubles") or 0) + (batting.get("triples") or 0) + (batting.get("homeRuns") or 0))
    if stat_key == "HHR":
        return float((batting.get("hits") or 0) + (batting.get("runs") or 0) + (batting.get("rbi") or 0))
    if stat_key == "BB":
        return float(batting.get("baseOnBalls") or 0)
    return 0.0


def is_final_game(status: str, live_state: str) -> bool:
    text = f"{status} {live_state}".lower()
    return "final" in text or "completed" in text


def live_state_for_feeds(feed_a: dict | None, feed_b: dict | None, fallback: str | None) -> str:
    states = [(feed or {}).get("live_state") for feed in (feed_a, feed_b) if feed]
    lowered = [str(s).lower() for s in states]
    if "live" in lowered:
        return "Live"
    if states and all("final" in s or "completed" in s for s in lowered):
        return "Final"
    return str(states[0] if states else fallback or "Preview")


def combined_matchup_status(status_a: str, state_a: str, status_b: str, state_b: str, fallback: str | None) -> str:
    final_a = is_final_game(status_a, state_a)
    final_b = is_final_game(status_b, state_b)
    if final_a and final_b:
        return "Final"
    if str(state_a).lower() == "live" or str(state_b).lower() == "live":
        return "Live"
    if final_a or final_b:
        return "Waiting for other game"
    return fallback or status_a or status_b or "Scheduled"


def combined_matchup_live_state(status_a: str, state_a: str, status_b: str, state_b: str, fallback: str | None) -> str:
    final_a = is_final_game(status_a, state_a)
    final_b = is_final_game(status_b, state_b)
    if final_a and final_b:
        return "Final"
    if str(state_a).lower() == "live" or str(state_b).lower() == "live":
        return "Live"
    if final_a or final_b:
        return "Waiting"
    return fallback or state_a or state_b or "Preview"


def settle_matchup_picks(conn: sqlite3.Connection, matchup_id: str) -> dict:
    m = conn.execute("SELECT * FROM matchups WHERE id = ?", (matchup_id,)).fetchone()
    if not m or m["status"] != "settled" or not m["winner_side"]:
        return {"settled": 0, "net_by_user": {}}
    slate = conn.execute("SELECT * FROM slates WHERE id = ?", (m["slate_id"],)).fetchone()
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (slate["league_id"],)).fetchone()
    net_by_user: dict[str, int] = {}
    settled = 0
    for p in conn.execute("SELECT * FROM picks WHERE matchup_id = ? AND status = 'open'", (matchup_id,)).fetchall():
        standing = conn.execute("SELECT * FROM standings WHERE league_id = ? AND user_id = ?", (slate["league_id"], p["user_id"])).fetchone()
        if not standing:
            conn.execute("INSERT OR IGNORE INTO standings (league_id, user_id, updated_at) VALUES (?, ?, ?)", (slate["league_id"], p["user_id"], now_iso()))
            standing = conn.execute("SELECT * FROM standings WHERE league_id = ? AND user_id = ?", (slate["league_id"], p["user_id"])).fetchone()
        won = p["side"] == m["winner_side"]
        if won:
            streak_bonus = min(1 + (league["streak_step"] / 100) * standing["streak"], 1 + league["streak_cap"] / 100)
            eff_mult = p["mult_at_lock"] + (league["margin_bonus"] if m["margin_bonus_hit"] else 0)
            payout = round(p["stake"] * eff_mult * streak_bonus)
            net_by_user[p["user_id"]] = net_by_user.get(p["user_id"], 0) + payout
            conn.execute(
                "UPDATE standings SET season = season + ?, streak = streak + 1, wins = wins + 1, updated_at = ? WHERE league_id = ? AND user_id = ?",
                (payout, now_iso(), slate["league_id"], p["user_id"]),
            )
            log_activity(conn, slate["league_id"], p["user_id"], "pick_won", f"Won {payout} pts on {matchup_label(conn, m)}", {"pick_id": p["id"], "matchup_id": p["matchup_id"]})
        else:
            payout = -p["stake"]
            net_by_user[p["user_id"]] = net_by_user.get(p["user_id"], 0) + payout
            conn.execute(
                "UPDATE standings SET season = season - ?, streak = 0, losses = losses + 1, updated_at = ? WHERE league_id = ? AND user_id = ?",
                (p["stake"], now_iso(), slate["league_id"], p["user_id"]),
            )
            log_activity(conn, slate["league_id"], p["user_id"], "pick_lost", f"Lost {p['stake']} pts on {matchup_label(conn, m)}", {"pick_id": p["id"], "matchup_id": p["matchup_id"]})
        conn.execute("UPDATE picks SET payout = ?, status = 'settled' WHERE id = ?", (payout, p["id"]))
        settled += 1
    return {"settled": settled, "net_by_user": net_by_user}


def matchup_label(conn: sqlite3.Connection, matchup: sqlite3.Row) -> str:
    row = conn.execute(
        """
        SELECT pa.name a_name, pb.name b_name
        FROM matchups m
        JOIN players pa ON pa.id = m.player_a_id
        JOIN players pb ON pb.id = m.player_b_id
        WHERE m.id = ?
        """,
        (matchup["id"],),
    ).fetchone()
    if not row:
        return "a settled pick"
    if matchup["winner_side"] == "tie":
        return f"{row['a_name']} / {row['b_name']} tie"
    return row["a_name"] if matchup["winner_side"] == "a" else row["b_name"]


def settle_slate_from_matchups(conn: sqlite3.Connection, slate_id: str) -> dict:
    slate = conn.execute("SELECT * FROM slates WHERE id = ?", (slate_id,)).fetchone()
    if not slate or slate["status"] != "open":
        return {"slate_id": slate_id, "net_by_user": {}}
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (slate["league_id"],)).fetchone()
    net_by_user: dict[str, int] = {}
    for m in conn.execute("SELECT * FROM matchups WHERE slate_id = ? AND status = 'settled'", (slate_id,)).fetchall():
        settled = settle_matchup_picks(conn, m["id"])
        for user_id, net in settled["net_by_user"].items():
            net_by_user[user_id] = net_by_user.get(user_id, 0) + net
    for p in conn.execute("SELECT user_id, COALESCE(SUM(payout),0) net FROM picks WHERE slate_id = ? AND status = 'settled' GROUP BY user_id", (slate_id,)).fetchall():
        net_by_user.setdefault(p["user_id"], int(p["net"] or 0))
    conn.execute("UPDATE slates SET status = 'settled' WHERE id = ?", (slate_id,))
    settle_playoff_round(conn, league, slate, net_by_user)
    return {"slate_id": slate_id, "net_by_user": net_by_user}


def side_totals(conn: sqlite3.Connection, matchup_id: str) -> dict:
    m = conn.execute("SELECT pub_a, pub_b, pub_tie FROM matchups WHERE id = ?", (matchup_id,)).fetchone()
    a, b, tie = int(m["pub_a"]), int(m["pub_b"]), int(m["pub_tie"])
    rows = conn.execute("SELECT side, stake FROM picks WHERE matchup_id = ? AND status = 'open'", (matchup_id,)).fetchall()
    for row in rows:
        if row["side"] == "a":
            a += row["stake"]
        elif row["side"] == "tie":
            tie += row["stake"]
        else:
            b += row["stake"]
    total = max(a + b + tie, 1)
    return {"a": a, "b": b, "tie": tie, "total": total, "pct_a": round(a * 100 / total), "pct_tie": round(tie * 100 / total)}


def multiplier(conn: sqlite3.Connection, league_id: str, matchup_id: str, side: str) -> float:
    if side not in ("a", "b", "tie"):
        raise ApiError(400, "Side must be a, b, or tie")
    league = conn.execute("SELECT min_mult, max_mult FROM leagues WHERE id = ?", (league_id,)).fetchone()
    totals = side_totals(conn, matchup_id)
    share = totals[side] / totals["total"]
    if share <= 0:
        return float(league["max_mult"])
    return round(max(float(league["min_mult"]), min(float(league["max_mult"]), 1 / share)), 2)


def matchup_locked(matchup: sqlite3.Row) -> bool:
    if matchup["status"] != "open":
        return True
    live_state = (matchup["live_state"] or "").lower()
    if live_state in ("live", "final"):
        return True
    game_start = matchup["game_start"]
    if not game_start:
        return False
    return datetime.now(timezone.utc) >= parse_utc(game_start)


def create_pick(conn: sqlite3.Connection, user_id: str, league_id: str, data: dict) -> dict:
    require_member(conn, user_id, league_id)
    require_fields(data, "matchup_id", "side", "stake")
    if data["side"] not in ("a", "b", "tie"):
        raise ApiError(400, "Side must be a, b, or tie")
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    stake = int(data["stake"])
    if stake < league["min_stake"] or stake > league["max_stake"]:
        raise ApiError(400, f"Stake must be between {league['min_stake']} and {league['max_stake']}")
    slate = conn.execute("SELECT * FROM slates WHERE league_id = ? AND status = 'open' ORDER BY week DESC LIMIT 1", (league_id,)).fetchone()
    if not slate:
        raise ApiError(404, "No open slate")
    matchup = conn.execute("SELECT * FROM matchups WHERE id = ? AND slate_id = ? AND status = 'open'", (data["matchup_id"], slate["id"])).fetchone()
    if not matchup:
        raise ApiError(404, "Matchup not found on active slate")
    if matchup_locked(matchup):
        raise ApiError(400, "This matchup is locked")
    staked = conn.execute(
        "SELECT COALESCE(SUM(stake),0) total FROM picks WHERE user_id = ? AND league_id = ? AND slate_id = ? AND status = 'open'",
        (user_id, league_id, slate["id"]),
    ).fetchone()["total"]
    if staked + stake > league["bankroll"]:
        raise ApiError(400, "Stake exceeds today's fresh slate bankroll")
    pick_id = new_id("pick")
    mult = multiplier(conn, league_id, matchup["id"], data["side"])
    try:
        conn.execute(
            """
            INSERT INTO picks (id, user_id, league_id, slate_id, matchup_id, side, stake, mult_at_lock, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pick_id, user_id, league_id, slate["id"], matchup["id"], data["side"], stake, mult, now_iso()),
        )
    except sqlite3.IntegrityError:
        raise ApiError(409, "You already picked this matchup")
    player = conn.execute(
        """
        SELECT pa.name a_name, pb.name b_name
        FROM matchups m
        JOIN players pa ON pa.id = m.player_a_id
        JOIN players pb ON pb.id = m.player_b_id
        WHERE m.id = ?
        """,
        (matchup["id"],),
    ).fetchone()
    side_name = "tie" if data["side"] == "tie" else player["a_name"] if data["side"] == "a" else player["b_name"]
    log_activity(
        conn,
        league_id,
        user_id,
        "pick_locked",
        f"Locked {stake} pts on {side_name}",
        {
            "matchup_id": matchup["id"],
            "side": data["side"],
            "side_name": side_name,
            "stake": stake,
            "stat_key": matchup["stat_key"],
        },
    )
    return row_to_dict(conn.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone())


def remove_pick(conn: sqlite3.Connection, user_id: str, league_id: str, pick_id: str) -> dict:
    pick = conn.execute(
        "SELECT * FROM picks WHERE id = ? AND user_id = ? AND league_id = ? AND status = 'open'",
        (pick_id, user_id, league_id),
    ).fetchone()
    if not pick:
        raise ApiError(404, "Open pick not found")
    matchup = conn.execute("SELECT * FROM matchups WHERE id = ?", (pick["matchup_id"],)).fetchone()
    if matchup and matchup_locked(matchup):
        raise ApiError(400, "This matchup is locked")
    conn.execute("DELETE FROM picks WHERE id = ?", (pick_id,))
    return {"removed": pick_id}


def user_picks(conn: sqlite3.Connection, user_id: str, league_id: str) -> dict:
    require_member(conn, user_id, league_id)
    rows = conn.execute(
        """
        SELECT p.* FROM picks p
        JOIN slates s ON s.id = p.slate_id
        WHERE p.user_id = ? AND p.league_id = ? AND s.status = 'open'
        ORDER BY p.created_at
        """,
        (user_id, league_id),
    ).fetchall()
    return {"picks": [row_to_dict(r) for r in rows]}


def leaderboard(conn: sqlite3.Connection, user_id: str, league_id: str) -> dict:
    require_member(conn, user_id, league_id)
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    rows = conn.execute(
        """
        SELECT s.*, u.handle, u.display_name, u.avatar_url, m.role, m.display_name league_display_name, m.avatar_url league_avatar_url
        FROM standings s
        JOIN users u ON u.id = s.user_id
        JOIN memberships m ON m.user_id = s.user_id AND m.league_id = s.league_id
        WHERE s.league_id = ?
        ORDER BY s.season DESC, s.wins DESC
        """,
        (league_id,),
    ).fetchall()
    return {
        "league": league_dict(conn, league, user_id),
        "rows": [
            {
                "rank": i + 1,
                "user_id": r["user_id"],
                "handle": r["handle"],
                "display_name": r["league_display_name"] or r["display_name"] or r["handle"],
                "avatar_url": r["league_avatar_url"] or r["avatar_url"],
                "role": r["role"],
                "season": r["season"],
                "streak": r["streak"],
                "wins": r["wins"],
                "losses": r["losses"],
                "accuracy": round(r["wins"] * 100 / (r["wins"] + r["losses"])) if (r["wins"] + r["losses"]) else None,
            }
            for i, r in enumerate(rows)
        ],
    }


def playoff_picture(conn: sqlite3.Connection, user_id: str, league_id: str) -> dict:
    require_member(conn, user_id, league_id)
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    board = leaderboard(conn, user_id, league_id)
    size = int(league["playoff_size"])
    seeds = board["rows"][:size]
    bubble = board["rows"][size : size + 8]
    if league["playoff_enabled"]:
        ensure_playoff_bracket(conn, league, board["rows"])
    rounds = playoff_rounds(conn, league_id, board["rows"])
    return {
        "league": league_dict(conn, league, user_id),
        "enabled": bool(league["playoff_enabled"]),
        "size": size,
        "season_weeks": int(league["season_weeks"]),
        "playoff_weeks": int(league["playoff_weeks"]),
        "seeds": seeds,
        "bubble": bubble,
        "rounds": rounds,
    }


def ensure_playoff_bracket(conn: sqlite3.Connection, league: sqlite3.Row, board_rows: list[dict]) -> None:
    if not league["playoff_enabled"]:
        return
    existing = conn.execute("SELECT COUNT(*) c FROM playoff_matchups WHERE league_id = ?", (league["id"],)).fetchone()["c"]
    if existing:
        return
    size = min(int(league["playoff_size"]), len(board_rows))
    if size < 2:
        return
    seeds = board_rows[:size]
    for idx in range(size // 2):
        high = seeds[idx]
        low = seeds[-(idx + 1)]
        conn.execute(
            """
            INSERT INTO playoff_matchups (
              id, league_id, round_no, matchup_no, week, seed_a, seed_b, user_a_id, user_b_id, created_at, updated_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("po"),
                league["id"],
                idx + 1,
                int(league["season_weeks"]) + 1,
                high["rank"],
                low["rank"],
                high["user_id"],
                low["user_id"],
                now_iso(),
                now_iso(),
            ),
        )


def playoff_rounds(conn: sqlite3.Connection, league_id: str, board_rows: list[dict]) -> list[dict]:
    board_by_user = {r["user_id"]: r for r in board_rows}
    rows = conn.execute(
        "SELECT * FROM playoff_matchups WHERE league_id = ? ORDER BY round_no, matchup_no",
        (league_id,),
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(int(row["round_no"]), []).append(row)
    rounds = []
    for round_no, matches in grouped.items():
        participant_count = len(matches) * 2
        rounds.append(
            {
                "round": round_no,
                "name": playoff_round_name(participant_count),
                "matchups": [playoff_matchup_dict(row, board_by_user) for row in matches],
            }
        )
    return rounds


def playoff_matchup_dict(row: sqlite3.Row, board_by_user: dict[str, dict]) -> dict:
    player_a = board_by_user.get(row["user_a_id"]) or playoff_placeholder(row["user_a_id"])
    player_b = board_by_user.get(row["user_b_id"]) or playoff_placeholder(row["user_b_id"])
    return {
        "id": row["id"],
        "round": row["round_no"],
        "matchup": row["matchup_no"],
        "week": row["week"],
        "seed_a": row["seed_a"],
        "seed_b": row["seed_b"],
        "player_a": player_a,
        "player_b": player_b,
        "score_a": row["score_a"],
        "score_b": row["score_b"],
        "winner_user_id": row["winner_user_id"],
        "status": row["status"],
    }


def playoff_placeholder(user_id: str | None) -> dict:
    return {"user_id": user_id, "handle": "TBD", "display_name": "TBD", "season": 0, "wins": 0, "losses": 0}


def settle_playoff_round(conn: sqlite3.Connection, league: sqlite3.Row, slate: sqlite3.Row, net_by_user: dict[str, int]) -> None:
    if not league["playoff_enabled"]:
        return
    rows = conn.execute(
        """
        SELECT * FROM playoff_matchups
        WHERE league_id = ? AND status = 'open'
        ORDER BY round_no, matchup_no
        """,
        (league["id"],),
    ).fetchall()
    if not rows:
        board_rows = leaderboard_for_system(conn, league["id"])
        ensure_playoff_bracket(conn, league, board_rows)
        rows = conn.execute(
            "SELECT * FROM playoff_matchups WHERE league_id = ? AND status = 'open' ORDER BY round_no, matchup_no",
            (league["id"],),
        ).fetchall()
    if not rows:
        return
    round_no = rows[0]["round_no"]
    current_round = [r for r in rows if r["round_no"] == round_no]
    for row in current_round:
        score_a = int(net_by_user.get(row["user_a_id"], 0))
        score_b = int(net_by_user.get(row["user_b_id"], 0))
        winner = row["user_a_id"] if score_a >= score_b else row["user_b_id"]
        conn.execute(
            """
            UPDATE playoff_matchups
            SET score_a = ?, score_b = ?, winner_user_id = ?, status = 'settled', updated_at = ?
            WHERE id = ?
            """,
            (score_a, score_b, winner, now_iso(), row["id"]),
        )
    settled = conn.execute(
        "SELECT * FROM playoff_matchups WHERE league_id = ? AND round_no = ? ORDER BY matchup_no",
        (league["id"], round_no),
    ).fetchall()
    winners = [r for r in settled if r["winner_user_id"]]
    if len(winners) <= 1 or len(winners) != len(settled):
        return
    next_exists = conn.execute(
        "SELECT 1 FROM playoff_matchups WHERE league_id = ? AND round_no = ?",
        (league["id"], round_no + 1),
    ).fetchone()
    if next_exists:
        return
    ordered = []
    for row in winners:
        if row["winner_user_id"] == row["user_a_id"]:
            ordered.append({"user_id": row["user_a_id"], "seed": row["seed_a"]})
        else:
            ordered.append({"user_id": row["user_b_id"], "seed": row["seed_b"]})
    ordered.sort(key=lambda x: x["seed"] or 999)
    for idx in range(len(ordered) // 2):
        high = ordered[idx]
        low = ordered[-(idx + 1)]
        conn.execute(
            """
            INSERT INTO playoff_matchups (
              id, league_id, round_no, matchup_no, week, seed_a, seed_b, user_a_id, user_b_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("po"),
                league["id"],
                round_no + 1,
                idx + 1,
                int(slate["week"]) + 1,
                high["seed"],
                low["seed"],
                high["user_id"],
                low["user_id"],
                now_iso(),
                now_iso(),
            ),
        )


def leaderboard_for_system(conn: sqlite3.Connection, league_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.*, u.handle, u.display_name, u.avatar_url, m.role, m.display_name league_display_name, m.avatar_url league_avatar_url
        FROM standings s
        JOIN users u ON u.id = s.user_id
        JOIN memberships m ON m.user_id = s.user_id AND m.league_id = s.league_id
        WHERE s.league_id = ?
        ORDER BY s.season DESC, s.wins DESC
        """,
        (league_id,),
    ).fetchall()
    return [
        {
            "rank": i + 1,
            "user_id": r["user_id"],
            "handle": r["handle"],
            "display_name": r["league_display_name"] or r["display_name"] or r["handle"],
            "avatar_url": r["league_avatar_url"] or r["avatar_url"],
            "role": r["role"],
            "season": r["season"],
            "streak": r["streak"],
            "wins": r["wins"],
            "losses": r["losses"],
        }
        for i, r in enumerate(rows)
    ]


def playoff_round_name(size: int) -> str:
    return {
        16: "Round of 16",
        14: "Opening Round",
        12: "Opening Round",
        10: "Opening Round",
        8: "Quarterfinals",
        6: "Opening Round",
        4: "Semifinals",
        2: "Championship",
    }.get(size, "Playoff Round")


def settle_demo(conn: sqlite3.Connection, user_id: str, league_id: str) -> dict:
    membership = require_member(conn, user_id, league_id)
    if membership["role"] != "owner":
        raise ApiError(403, "Only the league owner can settle a slate")
    league = conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()
    slate = conn.execute("SELECT * FROM slates WHERE league_id = ? AND status = 'open' ORDER BY week DESC LIMIT 1", (league_id,)).fetchone()
    if not slate:
        raise ApiError(404, "No open slate")
    for m in conn.execute("SELECT * FROM matchups WHERE slate_id = ?", (slate["id"],)).fetchall():
        totals = side_totals(conn, m["id"])
        p_a = max(0.3, min(0.7, 0.5 + ((totals["a"] / totals["total"]) - 0.5) * 0.4))
        winner = "a" if random.random() < p_a else "b"
        line = float(m["projection_line"])
        win_stat = round(line * random.uniform(0.85, 1.45) * 2) / 2
        lose_stat = round(min(win_stat - 0.5, line * random.uniform(0.45, 1.05)) * 2) / 2
        conn.execute(
            """
            UPDATE matchups
            SET winner_side = ?, actual_a = ?, actual_b = ?, margin_bonus_hit = ?, status = 'settled'
            WHERE id = ?
            """,
            (
                winner,
                win_stat if winner == "a" else lose_stat,
                win_stat if winner == "b" else lose_stat,
                1 if win_stat >= line * 1.2 else 0,
                m["id"],
            ),
        )
    net_by_user: dict[str, int] = {}
    for p in conn.execute("SELECT * FROM picks WHERE slate_id = ? AND status = 'open'", (slate["id"],)).fetchall():
        m = conn.execute("SELECT * FROM matchups WHERE id = ?", (p["matchup_id"],)).fetchone()
        standing = conn.execute("SELECT * FROM standings WHERE league_id = ? AND user_id = ?", (league_id, p["user_id"])).fetchone()
        won = p["side"] == m["winner_side"]
        if won:
            streak_bonus = min(1 + (league["streak_step"] / 100) * standing["streak"], 1 + league["streak_cap"] / 100)
            eff_mult = p["mult_at_lock"] + (league["margin_bonus"] if m["margin_bonus_hit"] else 0)
            payout = round(p["stake"] * eff_mult * streak_bonus)
            net_by_user[p["user_id"]] = net_by_user.get(p["user_id"], 0) + payout
            conn.execute(
                "UPDATE standings SET season = season + ?, streak = streak + 1, wins = wins + 1, updated_at = ? WHERE league_id = ? AND user_id = ?",
                (payout, now_iso(), league_id, p["user_id"]),
            )
        else:
            payout = -p["stake"]
            net_by_user[p["user_id"]] = net_by_user.get(p["user_id"], 0) + payout
            conn.execute(
                "UPDATE standings SET season = season - ?, streak = 0, losses = losses + 1, updated_at = ? WHERE league_id = ? AND user_id = ?",
                (p["stake"], now_iso(), league_id, p["user_id"]),
            )
        conn.execute("UPDATE picks SET payout = ?, status = 'settled' WHERE id = ?", (payout, p["id"]))
    conn.execute("UPDATE slates SET status = 'settled' WHERE id = ?", (slate["id"],))
    return {"slate_id": slate["id"], "net_by_user": net_by_user, "leaderboard": leaderboard(conn, user_id, league_id)}
