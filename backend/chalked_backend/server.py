from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import re
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .db import init_db, transaction
from .security import new_id
from .services import (
    ApiError,
    activity_feed,
    create_league,
    create_pick,
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
    playoff_picture,
    profile,
    public_user,
    refresh_active_slate,
    remove_pick,
    require_member,
    logout_other_sessions,
    request_email_verification,
    request_password_reset,
    session_rows,
    settle_due_slates,
    update_league_profile,
    update_profile,
    update_settings,
    user_picks,
    user_from_session,
)


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
        try:
            if method == "GET" and not parsed.path.startswith("/api/"):
                self.write_static(parsed.path)
                return
            for route_method, pattern, handler in ROUTES:
                match = re.fullmatch(pattern, parsed.path)
                if route_method == method and match:
                    result = handler(self, match.groupdict())
                    if isinstance(result, _AlreadyWritten):
                        return
                    self.write_json(result)
                    return
            raise ApiError(404, "Route not found")
        except ApiError as exc:
            self.write_json({"error": exc.message}, exc.status)
        except Exception as exc:
            self.write_json({"error": "Internal server error", "detail": str(exc)}, 500)

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
        self.end_headers()
        self.wfile.write(body)


def cookie_header(session_id: str, clear: bool = False) -> str:
    domain = os.environ.get("CHALKED_COOKIE_DOMAIN")
    domain_attr = f"; Domain={domain}" if domain else ""
    secure_attr = "; Secure" if cookie_secure_enabled() else ""
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
        return settle_due_slates(conn, force=True)


def register(req: RequestHandler, _: dict[str, str]) -> object:
    data = req.read_json()
    with transaction() as conn:
        ensure_seeded(conn)
        user = create_user(conn, data)
        public, session_id = login(conn, {"handle": data["handle"], "password": data["password"]}, req.session_meta())
        req.write_json({"user": public_user(user), "session_user": public}, cookie=cookie_header(session_id))
        return _AlreadyWritten()


def login_route(req: RequestHandler, _: dict[str, str]) -> object:
    with transaction() as conn:
        ensure_seeded(conn)
        user, session_id = login(conn, req.read_json(), req.session_meta())
        req.write_json({"user": user}, cookie=cookie_header(session_id))
        return _AlreadyWritten()


def logout(req: RequestHandler, _: dict[str, str]) -> object:
    session_id = req.session_id()
    with transaction() as conn:
        if session_id:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    req.write_json({"ok": True}, cookie=cookie_header("", clear=True))
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
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    filename = f"{new_id('img')}{ext}"
    target = (UPLOAD_ROOT / filename).resolve()
    if not str(target).startswith(str(UPLOAD_ROOT.resolve())):
        raise ApiError(400, "Invalid upload path")
    target.write_bytes(raw)
    return {"url": f"/uploads/{filename}"}


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


class _AlreadyWritten:
    pass


ROUTES: list[tuple[str, str, RouteHandler]] = [
    ("GET", r"/api/health", health),
    ("POST", r"/api/system/settle", settle_system_route),
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
    ("GET", r"/api/leagues", leagues),
    ("POST", r"/api/leagues", create_league_route),
    ("DELETE", r"/api/leagues/(?P<league_id>[^/]+)", delete_league_route),
    ("POST", r"/api/leagues/(?P<league_id>[^/]+)/join", join_league_route),
    ("PATCH", r"/api/leagues/(?P<league_id>[^/]+)/profile", update_league_profile_route),
    ("PATCH", r"/api/leagues/(?P<league_id>[^/]+)/settings", settings_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/slate", slate_route),
    ("POST", r"/api/leagues/(?P<league_id>[^/]+)/slate/refresh", refresh_slate_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/picks", picks_route),
    ("POST", r"/api/leagues/(?P<league_id>[^/]+)/picks", pick_route),
    ("DELETE", r"/api/leagues/(?P<league_id>[^/]+)/picks/(?P<pick_id>[^/]+)", delete_pick_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/leaderboard", leaderboard_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/playoffs", playoff_route),
    ("GET", r"/api/leagues/(?P<league_id>[^/]+)/activity", activity_route),
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
