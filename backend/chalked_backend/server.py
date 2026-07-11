from __future__ import annotations

import base64
import html
import hmac
import json
import logging
import mimetypes
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .db import db_path, init_db, transaction
from .security import new_id
from .share_cards import render_matchup_card_png, render_slate_results_card_png
from .storage import upload_backup, upload_image, upload_storage_status
from .services import (
    ApiError,
    admin_overview,
    activity_feed,
    check_rate_limit,
    create_league,
    create_matchup_chat,
    create_pick,
    create_feedback_report,
    create_user,
    change_password,
    confirm_email_verification,
    confirm_password_reset,
    delete_league,
    ensure_active_slate,
    ensure_seeded,
    join_league,
    leaderboard,
    list_leagues,
    login,
    matchup_chat,
    playoff_picture,
    profile,
    public_user,
    public_matchup_share,
    public_slate_share,
    refresh_active_slate,
    recent_slates,
    remove_pick,
    require_member,
    logout_other_sessions,
    leave_league,
    request_email_verification,
    request_password_reset,
    record_system_status,
    session_rows,
    set_user_moderation,
    settle_due_slates,
    update_league_profile,
    update_profile,
    update_settings,
    user_picks,
    user_from_session,
)


logging.basicConfig(level=os.environ.get("CHALKED_LOG_LEVEL", "WARNING").upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger(__name__)
HOST = os.environ.get("CHALKED_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHALKED_PORT") or os.environ.get("PORT") or "8080")
SESSION_COOKIE = os.environ.get("CHALKED_SESSION_COOKIE", "chalked_session")
STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
UPLOAD_ROOT = STATIC_ROOT / "uploads"


RouteHandler = Callable[["RequestHandler", dict[str, str]], object]


def production_enabled() -> bool:
    return os.environ.get("CHALKED_ENV", "").strip().lower() in {"prod", "production"} or os.environ.get("CHALKED_PUBLIC_URL", "").startswith("https://")


def cookie_secure_enabled() -> bool:
    value = os.environ.get("CHALKED_COOKIE_SECURE")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return production_enabled()


def allowed_origins() -> set[str]:
    raw = os.environ.get("CHALKED_ALLOWED_ORIGINS") or os.environ.get("CHALKED_PUBLIC_URL") or ""
    return {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ChalkedBackend/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def do_PATCH(self) -> None:
        self.dispatch("PATCH")

    def do_DELETE(self) -> None:
        self.dispatch("DELETE")

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("CHALKED_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    def dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        self.request_id = self.headers.get("x-request-id") or new_id("req")
        try:
            if method == "GET" and parsed.path.startswith("/share/matchup/"):
                self.write_matchup_share(parsed.path)
                return
            if method == "GET" and parsed.path.startswith("/share/slate/"):
                self.write_slate_share(parsed.path)
                return
            if method == "GET" and not parsed.path.startswith("/api/"):
                self.write_static(parsed.path)
                return
            for route_method, pattern, handler in ROUTES:
                match = re.fullmatch(pattern, parsed.path)
                if route_method == method and match:
                    self.enforce_rate_limit(method, parsed.path)
                    result = handler(self, match.groupdict())
                    if isinstance(result, _AlreadyWritten):
                        return
                    self.write_json(result)
                    return
            raise ApiError(404, "Route not found")
        except ApiError as exc:
            if exc.status >= 500:
                LOGGER.warning("api_error request_id=%s method=%s path=%s status=%s error=%s", self.request_id, method, parsed.path, exc.status, exc.message)
            self.write_json({"error": exc.message, "request_id": self.request_id}, exc.status)
        except Exception as exc:
            LOGGER.exception("unhandled_error request_id=%s method=%s path=%s", self.request_id, method, parsed.path)
            payload = {"error": "Internal server error", "request_id": self.request_id}
            if not production_enabled():
                payload["detail"] = str(exc)
            self.write_json(payload, 500)

    def enforce_rate_limit(self, method: str, path: str) -> None:
        rule = rate_limit_rule(method, path)
        if not rule:
            return
        forwarded = str(self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        host, *_ = self.client_address or ("",)
        ip = forwarded or host or "unknown"
        session = self.session_id() or "anon"
        key = f"{rule['scope']}:{ip if rule['scope'] == 'ip' else session}:{path}"
        with transaction() as conn:
            check_rate_limit(conn, key, rule["route"], rule["limit"], rule["window"])

    def write_static(self, path: str) -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_ROOT / rel).resolve()
        if not str(target).startswith(str(STATIC_ROOT.resolve())) or not target.is_file():
            raise ApiError(404, "Page not found")
        body = target.read_bytes()
        self.send_response(200)
        self.send_common_headers()
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_matchup_share(self, path: str) -> None:
        match = re.fullmatch(r"/share/matchup/(?P<matchup_id>[^/]+)(?P<card>/card\.png)?", path)
        if not match:
            raise ApiError(404, "Share link not found")
        pick_id = (self.query().get("pick") or [None])[0]
        with transaction() as conn:
            ensure_seeded(conn)
            share = public_matchup_share(conn, match.group("matchup_id"), pick_id)
        if match.group("card"):
            body = render_matchup_card_png(share, STATIC_ROOT)
            self.send_response(200)
            self.send_common_headers()
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        public = os.environ.get("CHALKED_PUBLIC_URL", "").strip().rstrip("/")
        if not public:
            proto = "https" if self.is_https() else "http"
            public = f"{proto}://{self.headers.get('host', '127.0.0.1:8080')}".rstrip("/")
        query_parts = [f"league={share['league_id']}"]
        if share.get("pick"):
            query_parts.append(f"pick={share['pick']['id']}")
        canonical = f"{public}/share/matchup/{share['id']}?{'&'.join(query_parts)}"
        image = f"{public}/share/matchup/{share['id']}/card.png?{'&'.join(query_parts)}&v={share['cache_key']}"
        html_text = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        replacements = {
            "title": f"{share['title']} | Chalked",
            "description": share["description"],
            "url": canonical,
            "image": image,
        }
        html_text = inject_share_meta(html_text, replacements)
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_slate_share(self, path: str) -> None:
        match = re.fullmatch(r"/share/slate/(?P<slate_id>[^/]+)(?P<card>/card\.png)?", path)
        if not match:
            raise ApiError(404, "Share link not found")
        user_id = (self.query().get("user") or [None])[0]
        with transaction() as conn:
            ensure_seeded(conn)
            share = public_slate_share(conn, match.group("slate_id"), user_id)
        if match.group("card"):
            body = render_slate_results_card_png(share, STATIC_ROOT)
            self.send_response(200)
            self.send_common_headers()
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        public = os.environ.get("CHALKED_PUBLIC_URL", "").strip().rstrip("/")
        if not public:
            proto = "https" if self.is_https() else "http"
            public = f"{proto}://{self.headers.get('host', '127.0.0.1:8080')}".rstrip("/")
        query = f"user={share['user_id']}"
        canonical = f"{public}/share/slate/{share['id']}?{query}"
        image = f"{public}/share/slate/{share['id']}/card.png?{query}&v={share['cache_key']}"
        html_text = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        replacements = {
            "title": f"{share['title']} | Chalked",
            "description": share["description"],
            "url": canonical,
            "image": image,
        }
        html_text = inject_share_meta(html_text, replacements)
        body = html_text.encode("utf-8")
        self.send_response(200)
        self.send_common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def read_json(self) -> dict:
        length = int(self.headers.get("content-length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(400, "Invalid JSON body")

    def session_id(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("cookie"))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def session_meta(self) -> dict:
        forwarded = str(self.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        host, *_ = self.client_address or ("",)
        return {
            "user_agent": self.headers.get("user-agent"),
            "ip_address": forwarded or host,
        }

    def is_https(self) -> bool:
        forwarded_proto = str(self.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        public = urlparse(os.environ.get("CHALKED_PUBLIC_URL") or "")
        host = str(self.headers.get("host") or "").split(":", 1)[0].lower()
        public_host = (public.hostname or "").lower()
        return (
            forwarded_proto == "https"
            or str(self.headers.get("x-forwarded-ssl") or "").lower() == "on"
            or (public.scheme == "https" and bool(public_host) and host == public_host)
        )

    def current_user(self) -> dict:
        with transaction() as conn:
            ensure_seeded(conn)
            user = user_from_session(conn, self.session_id())
            if not user:
                raise ApiError(401, "Login required")
            return user

    def send_common_headers(self) -> None:
        origin = self.headers.get("origin")
        allowed = allowed_origins()
        normalized_origin = origin.rstrip("/") if origin else ""
        if allowed:
            if normalized_origin in allowed:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        elif origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")

    def write_json(self, data: object, status: int = 200, cookie: str | None = None) -> None:
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if getattr(self, "request_id", None):
            self.send_header("X-Request-ID", self.request_id)
        self.end_headers()
        self.wfile.write(body)


def meta_escape(value: str) -> str:
    return html.escape(str(value), quote=True)


def inject_share_meta(html_text: str, meta: dict[str, str]) -> str:
    title = meta_escape(meta["title"])
    description = meta_escape(meta["description"])
    url = meta_escape(meta["url"])
    image = meta_escape(meta["image"])
    replacements = [
        (r"<title>.*?</title>", f"<title>{title}</title>"),
        (r'<meta name="description" content="[^"]*">', f'<meta name="description" content="{description}">'),
        (r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{url}">'),
        (r'<meta property="og:title" content="[^"]*">', f'<meta property="og:title" content="{title}">'),
        (r'<meta property="og:description" content="[^"]*">', f'<meta property="og:description" content="{description}">'),
        (r'<meta property="og:url" content="[^"]*">', f'<meta property="og:url" content="{url}">'),
        (r'<meta property="og:image" content="[^"]*">', f'<meta property="og:image" content="{image}">'),
        (r'<meta property="og:image:secure_url" content="[^"]*">', f'<meta property="og:image:secure_url" content="{image}">'),
        (r'<meta property="og:image:alt" content="[^"]*">', f'<meta property="og:image:alt" content="{title}">'),
        (r'<meta name="twitter:title" content="[^"]*">', f'<meta name="twitter:title" content="{title}">'),
        (r'<meta name="twitter:description" content="[^"]*">', f'<meta name="twitter:description" content="{description}">'),
        (r'<meta name="twitter:image" content="[^"]*">', f'<meta name="twitter:image" content="{image}">'),
        (r'<meta name="twitter:image:alt" content="[^"]*">', f'<meta name="twitter:image:alt" content="{title}">'),
    ]
    for pattern, replacement in replacements:
        html_text = re.sub(pattern, replacement, html_text, count=1, flags=re.DOTALL)
    return html_text


def rate_limit_rule(method: str, path: str) -> dict | None:
    if method == "OPTIONS" or path in {"/api/health"} or path.startswith("/api/system/"):
        return None
    if path in {"/api/auth/login", "/api/auth/register", "/api/auth/password-reset/request"}:
        return {"route": "auth", "scope": "ip", "limit": 8, "window": 60}
    if path in {"/api/auth/email/verify/request", "/api/uploads", "/api/feedback"}:
        return {"route": "account_write", "scope": "session", "limit": 20, "window": 3600}
    if method in {"POST", "PATCH", "DELETE"}:
        return {"route": "write", "scope": "session", "limit": 120, "window": 60}
    return None


def cookie_header(session_id: str, clear: bool = False, secure: bool | None = None) -> str:
    domain = os.environ.get("CHALKED_COOKIE_DOMAIN")
    domain_attr = f"; Domain={domain}" if domain else ""
    secure_attr = "; Secure" if (cookie_secure_enabled() if secure is None else secure) else ""
    if clear:
        return f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax{secure_attr}{domain_attr}; Max-Age=0"
    return f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Lax{secure_attr}{domain_attr}; Max-Age={30 * 24 * 60 * 60}"


def health(_: RequestHandler, __: dict[str, str]) -> dict:
    return {"ok": True, "service": "chalked-backend"}


def require_system_secret(req: RequestHandler) -> None:
    expected = os.environ.get("CHALKED_CRON_SECRET")
    if not expected:
        raise ApiError(404, "System jobs are not configured")
    bearer = req.headers.get("authorization", "")
    provided = ""
    if bearer.lower().startswith("bearer "):
        provided = bearer.split(" ", 1)[1].strip()
    provided = provided or req.headers.get("x-chalked-cron-secret", "").strip()
    if not hmac.compare_digest(provided, expected):
        raise ApiError(401, "Invalid system job secret")


def settle_system_route(req: RequestHandler, _: dict[str, str]) -> dict:
    require_system_secret(req)
    with transaction() as conn:
        ensure_seeded(conn)
        result = settle_due_slates(conn, force=True)
        record_system_status(
            conn,
            "settlement",
            {
                "ok": True,
                "result": result,
                "user_agent": req.headers.get("user-agent"),
            },
        )
        return result


def backup_system_route(req: RequestHandler, _: dict[str, str]) -> dict:
    require_system_secret(req)
    source_path = db_path()
    if not source_path.exists():
        raise ApiError(404, "Database file not found")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"chalked-{stamp}.sqlite3"
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / filename
        source = sqlite3.connect(source_path)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        raw = snapshot.read_bytes()
    try:
        stored = upload_backup(raw, filename, source_path.parent / "backups")
    except RuntimeError as exc:
        raise ApiError(502, str(exc))
    with transaction() as conn:
        status = record_system_status(
            conn,
            "backup",
            {
                "ok": True,
                "filename": filename,
                "bytes": len(raw),
                "storage": stored.get("storage"),
                "url": stored.get("url"),
            },
        )
    return {"backup": status, "stored": stored, "bytes": len(raw)}


def register(req: RequestHandler, _: dict[str, str]) -> object:
    data = req.read_json()
    if not data.get("accept_terms"):
        raise ApiError(400, "You must accept the Terms and Privacy Policy to create an account")
    with transaction() as conn:
        ensure_seeded(conn)
        user = create_user(conn, data)
        verification = request_email_verification(conn, user["id"]) if user.get("email") else None
        public, session_id = login(conn, {"handle": data["handle"], "password": data["password"]}, req.session_meta())
        req.write_json({"user": public_user(user), "session_user": public, "verification": verification}, cookie=cookie_header(session_id, secure=req.is_https()))
        return _AlreadyWritten()


def login_route(req: RequestHandler, _: dict[str, str]) -> object:
    with transaction() as conn:
        ensure_seeded(conn)
        user, session_id = login(conn, req.read_json(), req.session_meta())
        req.write_json({"user": user}, cookie=cookie_header(session_id, secure=req.is_https()))
        return _AlreadyWritten()


def logout(req: RequestHandler, _: dict[str, str]) -> object:
    session_id = req.session_id()
    with transaction() as conn:
        if session_id:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    req.write_json({"ok": True}, cookie=cookie_header("", clear=True, secure=req.is_https()))
    return _AlreadyWritten()


def me(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    return {"user": public_user(user)}


def sessions_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        return session_rows(conn, user["id"], req.session_id())


def logout_others_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        return logout_other_sessions(conn, user["id"], req.session_id())


def profile_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return profile(conn, user["id"])


def update_profile_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return update_profile(conn, user["id"], req.read_json())


def change_password_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        return change_password(conn, user["id"], req.read_json(), req.session_id())


def request_verify_email_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        return request_email_verification(conn, user["id"])


def confirm_verify_email_route(req: RequestHandler, _: dict[str, str]) -> dict:
    with transaction() as conn:
        return confirm_email_verification(conn, req.read_json().get("token"))


def request_password_reset_route(req: RequestHandler, _: dict[str, str]) -> dict:
    with transaction() as conn:
        ensure_seeded(conn)
        return request_password_reset(conn, req.read_json())


def confirm_password_reset_route(req: RequestHandler, _: dict[str, str]) -> dict:
    with transaction() as conn:
        ensure_seeded(conn)
        return confirm_password_reset(conn, req.read_json())


def update_league_profile_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return update_league_profile(conn, user["id"], params["league_id"], req.read_json())


def upload_route(req: RequestHandler, _: dict[str, str]) -> dict:
    req.current_user()
    data = req.read_json()
    data_url = str(data.get("data_url") or "")
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,(.+)", data_url, re.DOTALL)
    if not match:
        raise ApiError(400, "Upload a PNG, JPG, WEBP, or GIF image")
    mime, encoded = match.groups()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ApiError(400, "Invalid image data")
    if len(raw) > 3 * 1024 * 1024:
        raise ApiError(400, "Image must be under 3 MB")
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[mime]
    try:
        return upload_image(raw, mime, ext, UPLOAD_ROOT)
    except ValueError as exc:
        raise ApiError(400, str(exc))
    except RuntimeError as exc:
        raise ApiError(502, str(exc))


def feedback_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        return {"feedback": create_feedback_report(conn, user["id"], req.read_json(), req.session_meta())}


def leagues(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return list_leagues(conn, user["id"])


def create_league_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return {"league": create_league(conn, user["id"], req.read_json())}


def delete_league_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return delete_league(conn, user["id"], params["league_id"])


def leave_league_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return leave_league(conn, user["id"], params["league_id"])


def join_league_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    code = req.read_json().get("code")
    target = code or params["league_id"]
    with transaction() as conn:
        ensure_seeded(conn)
        return {"league": join_league(conn, user["id"], target)}


def settings_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return {"league": update_settings(conn, user["id"], params["league_id"], req.read_json())}


def slate_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        require_member(conn, user["id"], params["league_id"])
        return {"slate": ensure_active_slate(conn, params["league_id"])}


def slates_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return recent_slates(conn, user["id"], params["league_id"])


def refresh_slate_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return {"slate": refresh_active_slate(conn, user["id"], params["league_id"])}


def pick_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return {"pick": create_pick(conn, user["id"], params["league_id"], req.read_json())}


def picks_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return user_picks(conn, user["id"], params["league_id"])


def delete_pick_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return remove_pick(conn, user["id"], params["league_id"], params["pick_id"])


def leaderboard_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return leaderboard(conn, user["id"], params["league_id"])


def playoff_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return playoff_picture(conn, user["id"], params["league_id"])


def activity_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return activity_feed(conn, user["id"], params["league_id"])


def matchup_chat_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return matchup_chat(conn, user["id"], params["league_id"], params["matchup_id"])


def create_matchup_chat_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return {"message": create_matchup_chat(conn, user["id"], params["league_id"], params["matchup_id"], req.read_json())}


def admin_status_route(req: RequestHandler, _: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        data = admin_overview(conn, user["id"])
    data["storage"] = upload_storage_status()
    return data


def admin_blacklist_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return set_user_moderation(conn, user["id"], params["user_id"], "blacklisted", req.read_json().get("reason"))


def admin_clear_blacklist_route(req: RequestHandler, params: dict[str, str]) -> dict:
    user = req.current_user()
    with transaction() as conn:
        ensure_seeded(conn)
        return set_user_moderation(conn, user["id"], params["user_id"], "active")


class _AlreadyWritten:
    pass


ROUTES: list[tuple[str, str, RouteHandler]] = [
    ("GET", r"/api/health", health),
    ("POST", r"/api/system/settle", settle_system_route),
    ("POST", r"/api/system/backup", backup_system_route),
    ("POST", r"/api/auth/register", register),
    ("POST", r"/api/auth/login", login_route),
    ("POST", r"/api/auth/logout", logout),
    ("GET", r"/api/auth/sessions", sessions_route),
    ("POST", r"/api/auth/logout-others", logout_others_route),
    ("POST", r"/api/auth/password", change_password_route),
    ("POST", r"/api/auth/email/verify/request", request_verify_email_route),
    ("POST", r"/api/auth/email/verify/confirm", confirm_verify_email_route),
    ("POST", r"/api/auth/password-reset/request", request_password_reset_route),
    ("POST", r"/api/auth/password-reset/confirm", confirm_password_reset_route),
    ("GET", r"/api/me", me),
    ("GET", r"/api/profile", profile_route),
    ("PATCH", r"/api/profile", update_profile_route),
    ("POST", r"/api/uploads", upload_route),
    ("POST", r"/api/feedback", feedback_route),
    ("GET", r"/api/admin/status", admin_status_route),
    ("POST", r"/api/admin/users/(?P<user_id>[^/]+)/blacklist", admin_blacklist_route),
    ("DELETE", r"/api/admin/users/(?P<user_id>[^/]+)/blacklist", admin_clear_blacklist_route),
    ("GET", r"/api/leagues", leagues),
    ("POST", r"/api/leagues", create_league_route),
    ("DELETE", r"/api/leagues/(?P<league_id>[^/]+)", delete_league_route),
    ("DELETE", r"/api/leagues/(?P<league_id>[^/]+)/membership", leave_league_route),
    ("POST", r"/api/leagues/(?P<league_id>[^/]+)/join", join_league_route),
    ("PATCH", r"/api/leagues/(?P<league_id>[^/]+)/profile", update_league_profile_route),
    ("PATCH", r"/api/leagues/(?P<league_id>[^/]+)/settings", settings_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/slate", slate_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/slates", slates_route),
    ("POST", r"/api/leagues/(?P<league_id>[^/]+)/slate/refresh", refresh_slate_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/picks", picks_route),
    ("POST", r"/api/leagues/(?P<league_id>[^/]+)/picks", pick_route),
    ("DELETE", r"/api/leagues/(?P<league_id>[^/]+)/picks/(?P<pick_id>[^/]+)", delete_pick_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/leaderboard", leaderboard_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/playoffs", playoff_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/activity", activity_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/matchups/(?P<matchup_id>[^/]+)/chat", matchup_chat_route),
    ("POST", r"/api/leagues/(?P<league_id>[^/]+)/matchups/(?P<matchup_id>[^/]+)/chat", create_matchup_chat_route),
]


def run() -> None:
    init_db()
    with transaction() as conn:
        ensure_seeded(conn, sync_players=True)
    httpd = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"Chalked backend listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
