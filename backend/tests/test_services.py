import os
import tempfile
import unittest
from pathlib import Path

from backend.chalked_backend import services
from backend.chalked_backend.server import cookie_header
from backend.chalked_backend.storage import upload_image
from backend.chalked_backend.storage import upload_storage_status
from backend.chalked_backend.db import init_db, transaction
from backend.chalked_backend.providers import GameInfo, Player
from backend.chalked_backend.services import (
    ApiError,
    activity_feed,
    build_daily_eligibility,
    confirm_email_verification,
    confirm_password_reset,
    create_league,
    create_pick,
    create_user,
    ensure_active_slate,
    ensure_seeded,
    join_league,
    leave_league,
    leaderboard,
    login,
    player_stat_value,
    request_email_verification,
    request_password_reset,
    record_system_status,
    refresh_active_slate,
    set_user_moderation,
    settle_due_slates,
    admin_overview,
    update_profile,
    update_settings,
)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        os.environ["CHALKED_DISABLE_MLB"] = "1"
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.sqlite3"
        init_db(self.db_path)

    def tearDown(self):
        os.environ.pop("CHALKED_DISABLE_MLB", None)
        self.tmp.cleanup()

    def test_leaderboard_is_per_league(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "alice", "email": "alice@example.com", "password": "secret123"})
            league_a = create_league(conn, user["id"], {"name": "A League", "code": "ALEAGUE"})
            league_b = create_league(conn, user["id"], {"name": "B League", "code": "BLEAGUE"})
            conn.execute(
                "UPDATE standings SET season = 250 WHERE league_id = ? AND user_id = ?",
                (league_a["id"], user["id"]),
            )
            conn.execute(
                "UPDATE standings SET season = -90 WHERE league_id = ? AND user_id = ?",
                (league_b["id"], user["id"]),
            )

            board_a = leaderboard(conn, user["id"], league_a["id"])
            board_b = leaderboard(conn, user["id"], league_b["id"])

            self.assertEqual(board_a["league"]["name"], "A League")
            self.assertEqual(board_b["league"]["name"], "B League")
            self.assertEqual(board_a["rows"][0]["season"], 250)
            self.assertEqual(board_b["rows"][0]["season"], -90)

    def test_only_owner_can_update_settings(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            owner = create_user(conn, {"handle": "owner", "password": "secret123"})
            guest = create_user(conn, {"handle": "guest", "password": "secret123"})
            league = create_league(conn, owner["id"], {"name": "Owner League", "code": "OWNER"})
            join_league(conn, guest["id"], league["id"])

            updated = update_settings(conn, owner["id"], league["id"], {"bankroll": 1500, "min_stake": 20})
            self.assertEqual(updated["settings"]["bankroll"], 1500)
            self.assertEqual(updated["settings"]["min_stake"], 20)

            with self.assertRaises(ApiError) as ctx:
                update_settings(conn, guest["id"], league["id"], {"bankroll": 500})
            self.assertEqual(ctx.exception.status, 403)

    def test_member_can_leave_league_and_clear_their_data(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            owner = create_user(conn, {"handle": "leaveowner", "password": "secret123"})
            guest = create_user(conn, {"handle": "leaver", "password": "secret123"})
            league = create_league(conn, owner["id"], {"name": "Leave League", "code": "LEAVE"})
            join_league(conn, guest["id"], league["id"])
            slate = ensure_active_slate(conn, league["id"])
            create_pick(conn, guest["id"], league["id"], {"matchup_id": slate["matchups"][0]["id"], "side": "a", "stake": 10})

            result = leave_league(conn, guest["id"], league["id"])

            self.assertEqual(result["left"], league["id"])
            self.assertIsNone(conn.execute("SELECT 1 FROM memberships WHERE league_id = ? AND user_id = ?", (league["id"], guest["id"])).fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM standings WHERE league_id = ? AND user_id = ?", (league["id"], guest["id"])).fetchone())
            self.assertIsNone(conn.execute("SELECT 1 FROM picks WHERE league_id = ? AND user_id = ?", (league["id"], guest["id"])).fetchone())

            with self.assertRaises(ApiError) as owner_leave:
                leave_league(conn, owner["id"], league["id"])
            self.assertEqual(owner_leave.exception.status, 400)

    def test_pick_uses_league_limits(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "picker", "password": "secret123"})
            league = create_league(
                conn,
                user["id"],
                {"name": "Limits League", "code": "LIMITS", "min_stake": 25, "max_stake": 50, "bankroll": 100},
            )
            slate = ensure_active_slate(conn, league["id"])
            matchup_id = slate["matchups"][0]["id"]

            with self.assertRaises(ApiError) as low:
                create_pick(conn, user["id"], league["id"], {"matchup_id": matchup_id, "side": "a", "stake": 10})
            self.assertEqual(low.exception.status, 400)

            pick = create_pick(conn, user["id"], league["id"], {"matchup_id": matchup_id, "side": "a", "stake": 50})
            self.assertEqual(pick["stake"], 50)
            self.assertGreaterEqual(pick["mult_at_lock"], league["settings"]["min_mult"])

    def test_next_slate_advances_after_settled_week(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "weekly", "password": "secret123"})
            league = create_league(conn, user["id"], {"name": "Weekly League", "code": "WEEKLY"})
            first = ensure_active_slate(conn, league["id"])
            self.assertEqual(first["week"], 1)
            conn.execute("UPDATE slates SET status = 'settled' WHERE id = ?", (first["id"],))

            second = ensure_active_slate(conn, league["id"])

            self.assertNotEqual(first["id"], second["id"])
            self.assertEqual(second["week"], first["week"] + 1)

    def test_settled_open_slate_rolls_forward_to_new_active_slate(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "rollforward", "password": "secret123"})
            league = create_league(conn, user["id"], {"name": "Roll Forward", "code": "ROLLFWD"})
            first = ensure_active_slate(conn, league["id"])
            conn.execute("UPDATE slates SET status = 'settled' WHERE id = ?", (first["id"],))

            second = ensure_active_slate(conn, league["id"])

            self.assertEqual(second["status"], "open")
            self.assertNotEqual(second["id"], first["id"])

    def test_username_changes_do_not_create_old_login_aliases(self):
        with transaction(self.db_path) as conn:
            user = create_user(conn, {"handle": "switcher", "email": "switcher@example.com", "password": "secret123"})
            update_profile(conn, user["id"], {"handle": "newname"})

            public, _ = login(conn, {"login": "newname", "password": "secret123"})
            self.assertEqual(public["handle"], "newname")

            with self.assertRaises(ApiError) as old_login:
                login(conn, {"login": "switcher", "password": "secret123"})
            self.assertEqual(old_login.exception.status, 401)

            with self.assertRaises(ApiError) as cooldown:
                update_profile(conn, user["id"], {"handle": "anothername"})
            self.assertEqual(cooldown.exception.status, 429)

    def test_username_uniqueness_is_checked_case_insensitively(self):
        with transaction(self.db_path) as conn:
            create_user(conn, {"handle": "TakenName", "email": "taken@example.com", "password": "secret123"})
            with self.assertRaises(ApiError) as duplicate:
                create_user(conn, {"handle": "takenname", "email": "other@example.com", "password": "secret123"})
            self.assertEqual(duplicate.exception.status, 409)

    def test_email_verification_and_password_reset_use_one_time_tokens(self):
        with transaction(self.db_path) as conn:
            user = create_user(conn, {"handle": "mailme", "email": "mailme@example.com", "password": "secret123"})
            verify = request_email_verification(conn, user["id"])
            self.assertIn("verify=", verify["dev_link"])
            confirm_email_verification(conn, verify["dev_link"].split("verify=", 1)[1])
            verified = conn.execute("SELECT email_verified_at FROM users WHERE id = ?", (user["id"],)).fetchone()
            self.assertTrue(verified["email_verified_at"])

            reset = request_password_reset(conn, {"login": "mailme@example.com"})
            token = reset["dev_link"].split("reset=", 1)[1]
            confirm_password_reset(conn, {"token": token, "new_password": "newsecret123"})
            public, _ = login(conn, {"login": "mailme", "password": "newsecret123"})
            self.assertEqual(public["handle"], "mailme")

    def test_cookie_security_can_follow_request_scheme(self):
        old_public = os.environ.get("CHALKED_PUBLIC_URL")
        old_secure = os.environ.get("CHALKED_COOKIE_SECURE")
        try:
            os.environ["CHALKED_PUBLIC_URL"] = "https://playchalked.com"
            os.environ.pop("CHALKED_COOKIE_SECURE", None)

            local_cookie = cookie_header("local-session", secure=False)
            prod_cookie = cookie_header("prod-session", secure=True)

            self.assertNotIn("; Secure", local_cookie)
            self.assertIn("; Secure", prod_cookie)
        finally:
            if old_public is None:
                os.environ.pop("CHALKED_PUBLIC_URL", None)
            else:
                os.environ["CHALKED_PUBLIC_URL"] = old_public
            if old_secure is None:
                os.environ.pop("CHALKED_COOKIE_SECURE", None)
            else:
                os.environ["CHALKED_COOKIE_SECURE"] = old_secure

    def test_activity_feed_records_league_events(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            owner = create_user(conn, {"handle": "feedowner", "password": "secret123"})
            guest = create_user(conn, {"handle": "feedguest", "password": "secret123"})
            league = create_league(conn, owner["id"], {"name": "Feed League", "code": "FEEDME"})
            join_league(conn, guest["id"], league["id"])
            slate = ensure_active_slate(conn, league["id"])
            create_pick(conn, guest["id"], league["id"], {"matchup_id": slate["matchups"][0]["id"], "side": "a", "stake": 25})

            feed = activity_feed(conn, owner["id"], league["id"])
            kinds = [event["kind"] for event in feed["events"]]
            self.assertIn("league_created", kinds)
            self.assertIn("member_joined", kinds)
            self.assertIn("pick_locked", kinds)

    def test_admin_can_blacklist_and_clear_accounts(self):
        old_admins = os.environ.get("CHALKED_ADMIN_HANDLES")
        try:
            os.environ["CHALKED_ADMIN_HANDLES"] = "boss"
            with transaction(self.db_path) as conn:
                ensure_seeded(conn)
                admin = create_user(conn, {"handle": "boss", "password": "secret123"})
                user = create_user(conn, {"handle": "badacct", "password": "secret123"})
                record_system_status(conn, "settlement", {"ok": True, "result": {"checked": 1, "settled": 0}})

                set_user_moderation(conn, admin["id"], user["id"], "blacklisted", "testing blacklist")
                overview = admin_overview(conn, admin["id"])

                self.assertEqual(overview["counts"]["blacklisted"], 1)
                self.assertEqual(overview["cron"]["value"]["result"]["checked"], 1)
                with self.assertRaises(ApiError) as blocked:
                    login(conn, {"login": "badacct", "password": "secret123"})
                self.assertEqual(blocked.exception.status, 403)

                set_user_moderation(conn, admin["id"], user["id"], "active")
                public, _ = login(conn, {"login": "badacct", "password": "secret123"})
                self.assertEqual(public["handle"], "badacct")
        finally:
            if old_admins is None:
                os.environ.pop("CHALKED_ADMIN_HANDLES", None)
            else:
                os.environ["CHALKED_ADMIN_HANDLES"] = old_admins

    def test_pick_locks_per_matchup_not_whole_slate(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "lockpick", "password": "secret123"})
            league = create_league(conn, user["id"], {"name": "Lock League", "code": "LOCKS"})
            slate = ensure_active_slate(conn, league["id"])
            locked = slate["matchups"][0]["id"]
            open_matchup = slate["matchups"][1]["id"]
            conn.execute(
                "UPDATE matchups SET game_start = datetime('now', '-5 minutes'), live_state = 'Live' WHERE id = ?",
                (locked,),
            )
            conn.execute(
                "UPDATE matchups SET game_start = datetime('now', '+2 hours'), live_state = 'Preview' WHERE id = ?",
                (open_matchup,),
            )

            with self.assertRaises(ApiError) as locked_error:
                create_pick(conn, user["id"], league["id"], {"matchup_id": locked, "side": "a", "stake": 25})
            self.assertEqual(locked_error.exception.status, 400)

            pick = create_pick(conn, user["id"], league["id"], {"matchup_id": open_matchup, "side": "a", "stake": 25})
            self.assertEqual(pick["matchup_id"], open_matchup)

    def test_daily_eligibility_excludes_static_pitcher_who_is_not_probable(self):
        os.environ.pop("CHALKED_DISABLE_MLB", None)
        games = [
            GameInfo(
                "game-atl-sea",
                "2026-07-08",
                "2026-07-08T22:40:00+00:00",
                "Scheduled",
                None,
                "Preview",
                ("ATL", "SEA"),
                (
                    Player("mlb_999111", "999111", "Actual Atlanta Starter", "ATL", "SP", "K"),
                    Player("mlb_669302", "669302", "Logan Gilbert", "SEA", "SP", "K"),
                ),
            )
        ]
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            services.sync_probable_pitchers(conn, games)
            players = conn.execute("SELECT * FROM players WHERE active = 1").fetchall()

            eligible = build_daily_eligibility(players, games)
            k_names = {e.player["name"] for e in eligible if e.player["stat_group"] == "K"}
            k_ids = {e.player["id"] for e in eligible if e.player["stat_group"] == "K"}

            self.assertNotIn("Chris Sale", k_names)
            self.assertEqual(k_ids, {"mlb_999111", "mlb_669302"})

    def test_k_matchups_use_only_mlb_probable_starter_rows(self):
        os.environ.pop("CHALKED_DISABLE_MLB", None)
        games = [
            GameInfo(
                "game-atl-sea",
                "2026-07-08",
                "2026-07-08T22:40:00+00:00",
                "Scheduled",
                None,
                "Preview",
                ("ATL", "SEA"),
                (
                    Player("mlb_999111", "999111", "Actual Atlanta Starter", "ATL", "SP", "K"),
                    Player("mlb_669302", "669302", "Logan Gilbert", "SEA", "SP", "K"),
                ),
            )
        ]
        original_loader = services.load_game_schedule
        services.load_game_schedule = lambda _players, force_refresh=False: games
        try:
            with transaction(self.db_path) as conn:
                ensure_seeded(conn)
                user = create_user(conn, {"handle": "eligible", "password": "secret123"})
                league = create_league(conn, user["id"], {"name": "Eligibility", "code": "ELIGIBLE", "matchups_per_slate": 6})
                slate = ensure_active_slate(conn, league["id"])
                k_rows = [m for m in slate["matchups"] if m["stat_key"] == "K"]

                self.assertEqual(len(k_rows), 1)
                self.assertTrue(k_rows[0]["players"]["a"]["id"].startswith("mlb_"))
                self.assertTrue(k_rows[0]["players"]["b"]["id"].startswith("mlb_"))
                names = {k_rows[0]["players"]["a"]["name"], k_rows[0]["players"]["b"]["name"]}
                self.assertNotIn("Chris Sale", names)
        finally:
            services.load_game_schedule = original_loader
            os.environ["CHALKED_DISABLE_MLB"] = "1"

    def test_no_probable_pitchers_means_no_padded_k_matchups(self):
        os.environ.pop("CHALKED_DISABLE_MLB", None)
        games = [
            GameInfo("game-atl-sea", "2026-07-08", "2026-07-08T22:40:00+00:00", "Scheduled", None, "Preview", ("ATL", "SEA"), ())
        ]
        original_loader = services.load_game_schedule
        services.load_game_schedule = lambda _players, force_refresh=False: games
        try:
            with transaction(self.db_path) as conn:
                ensure_seeded(conn)
                user = create_user(conn, {"handle": "shortslate", "password": "secret123"})
                league = create_league(conn, user["id"], {"name": "Short Slate", "code": "SHORTY", "matchups_per_slate": 6})
                slate = ensure_active_slate(conn, league["id"])

                self.assertFalse([m for m in slate["matchups"] if m["stat_key"] == "K"])
                self.assertLessEqual(len(slate["matchups"]), 6)
        finally:
            services.load_game_schedule = original_loader
            os.environ["CHALKED_DISABLE_MLB"] = "1"

    def test_manual_slate_refresh_bypasses_schedule_cache(self):
        calls = []
        original_cached = services.cached_mlb_schedule

        def fake_schedule(target_date, force_refresh=False):
            calls.append(force_refresh)
            return []

        services.cached_mlb_schedule = fake_schedule
        try:
            with transaction(self.db_path) as conn:
                os.environ["CHALKED_DISABLE_MLB"] = "1"
                user = create_user(conn, {"handle": "refreshowner", "password": "secret123"})
                league = create_league(conn, user["id"], {"name": "Refresh League", "code": "REFRESH"})
                before = conn.execute(
                    "SELECT COUNT(*) c FROM matchups m JOIN slates s ON s.id = m.slate_id WHERE s.league_id = ?",
                    (league["id"],),
                ).fetchone()["c"]
            os.environ.pop("CHALKED_DISABLE_MLB", None)
            calls.clear()
            with self.assertRaises(ApiError) as ctx:
                with transaction(self.db_path) as conn:
                    refresh_active_slate(conn, user["id"], league["id"])
            self.assertEqual(ctx.exception.status, 503)
            with transaction(self.db_path) as conn:
                after = conn.execute(
                    "SELECT COUNT(*) c FROM matchups m JOIN slates s ON s.id = m.slate_id WHERE s.league_id = ?",
                    (league["id"],),
                ).fetchone()["c"]
            self.assertEqual(after, before)
            self.assertIn(True, calls)
        finally:
            services.cached_mlb_schedule = original_cached
            os.environ["CHALKED_DISABLE_MLB"] = "1"

    def test_confirmed_lineup_filters_batter_eligibility(self):
        os.environ.pop("CHALKED_DISABLE_MLB", None)
        games = [
            GameInfo("game-atl-sea", "2026-07-08", "2026-07-08T22:40:00+00:00", "Scheduled", None, "Preview", ("ATL", "SEA"), ())
        ]
        original_cached_feed = services.cached_live_feed
        services.cached_live_feed = lambda _game_pk: {
            "lineups": {
                "ATL": [{"id": "660670", "name": "Ronald Acuna Jr.", "team": "ATL", "position": "RF", "batting_order": 100}],
                "SEA": [{"id": "677594", "name": "Julio Rodriguez", "team": "SEA", "position": "CF", "batting_order": 100}],
            }
        }
        try:
            with transaction(self.db_path) as conn:
                ensure_seeded(conn)
                services.sync_lineup_batters(conn, games)
                players = conn.execute("SELECT * FROM players WHERE active = 1").fetchall()
                eligible = build_daily_eligibility(players, games)
                batter_names = {e.player["name"] for e in eligible if e.player["stat_group"] != "K"}

                self.assertIn("Ronald Acuna Jr.", batter_names)
                self.assertIn("Julio Rodriguez", batter_names)
                self.assertNotIn("Matt Olson", batter_names)
                self.assertTrue(all(e.confidence == "confirmed" for e in eligible if e.player["stat_group"] != "K"))
        finally:
            services.cached_live_feed = original_cached_feed
            os.environ["CHALKED_DISABLE_MLB"] = "1"

    def test_api_matchup_players_include_external_ids_for_headshots(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "photos", "password": "secret123"})
            league = create_league(conn, user["id"], {"name": "Photos", "code": "PHOTOS"})
            slate = ensure_active_slate(conn, league["id"])
            first = slate["matchups"][0]

            self.assertTrue(first["players"]["a"]["external_id"])
            self.assertTrue(first["players"]["b"]["external_id"])

    def test_player_stat_value_matches_matchup_stat_rules(self):
        stat = {
            "strikeOuts": 7,
            "totalBases": 5,
            "hits": 2,
            "baseOnBalls": 1,
            "homeRuns": 1,
            "runs": 2,
            "stolenBases": 1,
            "rbi": 3,
        }

        self.assertEqual(player_stat_value("K", stat), 7)
        self.assertEqual(player_stat_value("TB", stat), 5)
        self.assertEqual(player_stat_value("OB", stat), 3)
        self.assertEqual(player_stat_value("HR", stat), 1)
        self.assertEqual(player_stat_value("SPD", stat), 3)
        self.assertEqual(player_stat_value("H", stat), 2)
        self.assertEqual(player_stat_value("R", stat), 2)
        self.assertEqual(player_stat_value("RBI", stat), 3)

    def test_api_matchup_includes_last5_arrays(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "lastfive", "password": "secret123"})
            league = create_league(conn, user["id"], {"name": "Last Five", "code": "LAST5"})
            slate = ensure_active_slate(conn, league["id"])
            first = slate["matchups"][0]

            self.assertIn("last5", first)
            self.assertIn("a", first["last5"])
            self.assertIn("b", first["last5"])

    def test_seeded_market_points_lean_to_stronger_last5(self):
        state = services.random.getstate()
        try:
            services.random.seed(11)
            market = services.seeded_market_points("K", [9, 8, 8, 7, 9], [3, 4, 3, 4, 2])

            self.assertGreater(market["a"], market["b"])
            self.assertGreater(market["tie"], 0)
        finally:
            services.random.setstate(state)

    def test_seeded_market_points_give_close_matchups_more_tie_interest(self):
        state = services.random.getstate()
        try:
            services.random.seed(23)
            close = services.seeded_market_points("H", [1, 2, 1, 2, 1], [2, 1, 2, 1, 2])
            services.random.seed(23)
            lopsided = services.seeded_market_points("H", [4, 4, 3, 5, 4], [0, 1, 0, 1, 0])

            self.assertGreater(close["tie"], lopsided["tie"])
        finally:
            services.random.setstate(state)

    def test_settle_due_slates_scans_open_slates(self):
        with transaction(self.db_path) as conn:
            ensure_seeded(conn)
            user = create_user(conn, {"handle": "cronsettle", "password": "secret123"})
            create_league(conn, user["id"], {"name": "Cron Settle", "code": "CRONSET"})

            result = settle_due_slates(conn)

            self.assertGreaterEqual(result["checked"], 1)
            self.assertIn("settled", result)

    def test_upload_image_uses_local_fallback_without_object_storage_env(self):
        keys = [
            "CHALKED_UPLOAD_BUCKET",
            "CHALKED_UPLOAD_ENDPOINT",
            "CHALKED_UPLOAD_ACCESS_KEY_ID",
            "CHALKED_UPLOAD_SECRET_ACCESS_KEY",
            "CHALKED_REQUIRE_OBJECT_STORAGE",
            "CHALKED_ENV",
        ]
        old = {key: os.environ.pop(key, None) for key in keys}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                result = upload_image(b"fake-image", "image/png", ".png", Path(tmp))
                self.assertEqual(result["storage"], "local")
                self.assertTrue(result["url"].startswith("/uploads/img_"))
                self.assertTrue(any(Path(tmp).iterdir()))
        finally:
            for key, value in old.items():
                if value is not None:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)

    def test_production_uploads_require_object_storage(self):
        keys = [
            "CHALKED_UPLOAD_BUCKET",
            "CHALKED_UPLOAD_ENDPOINT",
            "CHALKED_UPLOAD_ACCESS_KEY_ID",
            "CHALKED_UPLOAD_SECRET_ACCESS_KEY",
            "CHALKED_REQUIRE_OBJECT_STORAGE",
            "CHALKED_ENV",
        ]
        old = {key: os.environ.pop(key, None) for key in keys}
        try:
            os.environ["CHALKED_ENV"] = "production"
            status = upload_storage_status()
            self.assertTrue(status["required"])
            self.assertFalse(status["local_allowed"])
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError):
                    upload_image(b"fake-image", "image/png", ".png", Path(tmp))
        finally:
            for key, value in old.items():
                if value is not None:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
