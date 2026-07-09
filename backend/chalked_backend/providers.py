from __future__ import annotations

import json
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable


@dataclass(frozen=True)
class Player:
    id: str
    external_id: str
    name: str
    team: str
    position: str
    stat_group: str


@dataclass(frozen=True)
class PlayerEligibility:
    player: Player | object
    game: "GameInfo"
    opponent: str
    role: str
    reason: str
    confidence: str


@dataclass(frozen=True)
class GameInfo:
    game_pk: str
    game_date: str
    start_time: str
    status: str
    inning: str | None
    live_state: str
    teams: tuple[str, str]
    probable_pitchers: tuple[Player, ...] = ()


STAT_RULES = {
    "K": {"label": "Strikeouts - Game", "unit": "K", "line": (8, 11)},
    "BF": {"label": "Batters Faced - Game", "unit": "BF", "line": (22, 30)},
    "IP": {"label": "Innings Pitched - Game", "unit": "IP", "line": (5, 7)},
    "TB": {"label": "Total Bases - Game", "unit": "TB", "line": (9, 13)},
    "OB": {"label": "Hits + Walks - Game", "unit": "H+BB", "line": (7, 10)},
    "HR": {"label": "Home Runs - Game", "unit": "HR", "line": (1.5, 2.5)},
    "SPD": {"label": "Runs + Steals - Game", "unit": "R+SB", "line": (5, 8)},
    "H": {"label": "Hits - Game", "unit": "H", "line": (6, 9)},
    "R": {"label": "Runs Scored - Game", "unit": "R", "line": (5, 8)},
    "RBI": {"label": "RBI - Game", "unit": "RBI", "line": (6, 9)},
    "XBH": {"label": "Extra-Base Hits - Game", "unit": "XBH", "line": (1.5, 3.5)},
    "HHR": {"label": "Hits + Runs + RBI - Game", "unit": "H+R+RBI", "line": (6, 11)},
    "BB": {"label": "Walks - Game", "unit": "BB", "line": (2, 5)},
}


STATIC_PLAYERS = [
    Player("skenes", "694973", "Paul Skenes", "PIT", "SP", "K"),
    Player("skubal", "669373", "Tarik Skubal", "DET", "SP", "K"),
    Player("wheeler", "554430", "Zack Wheeler", "PHI", "SP", "K"),
    Player("crochet", "676979", "Garrett Crochet", "BOS", "SP", "K"),
    Player("gilbert", "669302", "Logan Gilbert", "SEA", "SP", "K"),
    Player("sale", "519242", "Chris Sale", "ATL", "SP", "K"),
    Player("judge", "592450", "Aaron Judge", "NYY", "RF", "TB"),
    Player("ohtani", "660271", "Shohei Ohtani", "LAD", "DH", "TB"),
    Player("gunnar", "683002", "Gunnar Henderson", "BAL", "SS", "TB"),
    Player("tucker", "663656", "Kyle Tucker", "CHC", "RF", "TB"),
    Player("soto", "665742", "Juan Soto", "NYM", "RF", "OB"),
    Player("harper", "547180", "Bryce Harper", "PHI", "1B", "OB"),
    Player("vladdy", "665489", "Vladimir Guerrero Jr.", "TOR", "1B", "OB"),
    Player("witt", "677951", "Bobby Witt Jr.", "KC", "SS", "SPD"),
    Player("elly", "682829", "Elly De La Cruz", "CIN", "SS", "SPD"),
    Player("schwarber", "656941", "Kyle Schwarber", "PHI", "DH", "HR"),
    Player("alvarez", "670541", "Yordan Alvarez", "HOU", "DH", "HR"),
    Player("acuna", "660670", "Ronald Acuna Jr.", "ATL", "RF", "H"),
    Player("carroll", "682998", "Corbin Carroll", "AZ", "CF", "H"),
    Player("freeman", "518692", "Freddie Freeman", "LAD", "1B", "H"),
    Player("betts", "605141", "Mookie Betts", "LAD", "SS", "R"),
    Player("turner", "607208", "Trea Turner", "PHI", "SS", "R"),
    Player("lindor", "596019", "Francisco Lindor", "NYM", "SS", "R"),
    Player("julio", "677594", "Julio Rodriguez", "SEA", "CF", "R"),
    Player("jram", "608070", "Jose Ramirez", "CLE", "3B", "RBI"),
    Player("alonso", "624413", "Pete Alonso", "NYM", "1B", "RBI"),
    Player("olson", "621566", "Matt Olson", "ATL", "1B", "RBI"),
    Player("devers", "646240", "Rafael Devers", "SF", "3B", "RBI"),
]


class PlayerProvider:
    def players(self) -> Iterable[Player]:
        raise NotImplementedError


class StaticPlayerProvider(PlayerProvider):
    def players(self) -> Iterable[Player]:
        return STATIC_PLAYERS


class MlbStatsProvider(PlayerProvider):
    """Pull current player metadata from MLB StatsAPI.

    The curated IDs keep early slates high-signal while the names, teams, and
    positions come from the live MLB feed. If the feed is unavailable, callers
    can fall back to StaticPlayerProvider without breaking login or slates.
    """

    PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people?personIds={ids}&hydrate=currentTeam"

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout

    def players(self) -> Iterable[Player]:
        ids = ",".join(p.external_id for p in STATIC_PLAYERS)
        request = urllib.request.Request(self.PEOPLE_URL.format(ids=ids), headers={"User-Agent": "Chalked/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("MLB StatsAPI unavailable") from exc

        by_external = {p.external_id: p for p in STATIC_PLAYERS}
        players: list[Player] = []
        for person in payload.get("people", []):
            external_id = str(person.get("id", ""))
            fallback = by_external.get(external_id)
            if not fallback:
                continue
            team = (person.get("currentTeam") or {}).get("abbreviation") or fallback.team
            position = (person.get("primaryPosition") or {}).get("abbreviation") or fallback.position
            players.append(
                Player(
                    fallback.id,
                    external_id,
                    person.get("fullName") or fallback.name,
                    team,
                    position,
                    fallback.stat_group,
                )
            )
        if len(players) < len(STATIC_PLAYERS) // 2:
            raise RuntimeError("MLB StatsAPI returned too few players")
        return players


class MlbScheduleProvider:
    SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}&hydrate=team,linescore,probablePitcher"

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout

    def schedule(self, target_date: date | None = None) -> list[GameInfo]:
        target = target_date or datetime.now(timezone.utc).date()
        request = urllib.request.Request(
            self.SCHEDULE_URL.format(date=target.isoformat()),
            headers={"User-Agent": "Chalked/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("MLB schedule unavailable") from exc

        games: list[GameInfo] = []
        for day in payload.get("dates", []):
            for game in day.get("games", []):
                away = ((game.get("teams") or {}).get("away") or {}).get("team") or {}
                home = ((game.get("teams") or {}).get("home") or {}).get("team") or {}
                away_probable = ((game.get("teams") or {}).get("away") or {}).get("probablePitcher") or {}
                home_probable = ((game.get("teams") or {}).get("home") or {}).get("probablePitcher") or {}
                status = (game.get("status") or {}).get("detailedState") or "Scheduled"
                coded = (game.get("status") or {}).get("abstractGameState") or "Preview"
                linescore = game.get("linescore") or {}
                inning = format_inning(linescore)
                away_abbr = away.get("abbreviation") or away.get("name") or ""
                home_abbr = home.get("abbreviation") or home.get("name") or ""
                probable_pitchers = []
                if away_probable.get("id") and away_probable.get("fullName"):
                    probable_pitchers.append(Player(f"mlb_{away_probable['id']}", str(away_probable["id"]), away_probable["fullName"], away_abbr, "SP", "K"))
                if home_probable.get("id") and home_probable.get("fullName"):
                    probable_pitchers.append(Player(f"mlb_{home_probable['id']}", str(home_probable["id"]), home_probable["fullName"], home_abbr, "SP", "K"))
                games.append(
                    GameInfo(
                        str(game.get("gamePk")),
                        day.get("date") or target.isoformat(),
                        game.get("gameDate"),
                        status,
                        inning,
                        coded,
                        (away_abbr, home_abbr),
                        tuple(probable_pitchers),
                    )
                )
        if not games:
            raise RuntimeError("No MLB games found for date")
        return sorted(games, key=lambda g: g.start_time or "")


class MlbLiveFeedProvider:
    FEED_URL = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout

    def game(self, game_pk: str) -> dict:
        if not game_pk or game_pk.startswith("static-"):
            raise RuntimeError("Live feed unavailable for static game")
        request = urllib.request.Request(
            self.FEED_URL.format(game_pk=game_pk),
            headers={"User-Agent": "Chalked/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("MLB live feed unavailable") from exc

        game_data = payload.get("gameData") or {}
        live_data = payload.get("liveData") or {}
        status = game_data.get("status") or {}
        linescore = live_data.get("linescore") or {}
        players: dict[str, dict] = {}
        lineups: dict[str, list[dict]] = {}
        for team in ((live_data.get("boxscore") or {}).get("teams") or {}).values():
            team_info = team.get("team") or {}
            team_abbr = team_info.get("abbreviation") or team_info.get("name") or ""
            lineup_players: list[dict] = []
            for player in (team.get("players") or {}).values():
                person = player.get("person") or {}
                player_id = str(person.get("id") or "").strip()
                if player_id:
                    players[player_id] = player.get("stats") or {}
                    batting_order = str(player.get("battingOrder") or "")
                    if batting_order.isdigit() and int(batting_order) % 100 == 0:
                        position = (player.get("position") or {}).get("abbreviation") or ""
                        lineup_players.append(
                            {
                                "id": player_id,
                                "name": person.get("fullName") or f"MLB {player_id}",
                                "team": team_abbr,
                                "position": position,
                                "batting_order": int(batting_order),
                            }
                        )
            if team_abbr and lineup_players:
                lineups[team_abbr] = sorted(lineup_players, key=lambda p: p["batting_order"])
        return {
            "status": status.get("detailedState") or "Scheduled",
            "live_state": status.get("abstractGameState") or "Preview",
            "inning": format_inning(linescore),
            "players": players,
            "lineups": lineups,
        }


def format_inning(linescore: dict) -> str | None:
    ordinal = linescore.get("currentInningOrdinal")
    half = linescore.get("inningHalf")
    if ordinal and half:
        return f"{half} {ordinal}"
    return ordinal


class MlbGameLogProvider:
    STATS_URL = "https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=gameLog&group={group}&season={season}"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def game_log(self, player_id: str, group: str, season: int | None = None) -> list[dict]:
        target_season = season or datetime.now(timezone.utc).year
        request = urllib.request.Request(
            self.STATS_URL.format(player_id=player_id, group=group, season=target_season),
            headers={"User-Agent": "Chalked/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError("MLB game log unavailable") from exc
        splits = (payload.get("stats") or [{}])[0].get("splits") or []
        return sorted(splits, key=lambda split: split.get("date") or "", reverse=True)


def static_schedule(players: Iterable[Player], target_date: date | None = None) -> list[GameInfo]:
    target = target_date or datetime.now(timezone.utc).date()
    teams = sorted({p.team for p in players})
    base = datetime.now(timezone.utc) + timedelta(hours=2)
    games: list[GameInfo] = []
    for idx in range(0, len(teams), 2):
        away = teams[idx]
        home = teams[(idx + 1) % len(teams)]
        start = base + timedelta(minutes=35 * (idx // 2))
        games.append(GameInfo(f"static-{idx}", target.isoformat(), start.isoformat(), "Scheduled", None, "Preview", (away, home)))
    return games


def matchup_line(stat_group: str) -> float:
    lo, hi = STAT_RULES[stat_group]["line"]
    return round(random.uniform(lo, hi) * 2) / 2
