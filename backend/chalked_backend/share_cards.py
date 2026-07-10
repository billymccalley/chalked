from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


W, H = 1200, 630
GOLD = "#FFC53D"
BG = "#080D13"
CARD = "#111923"
PANEL = "#0D141D"
LINE = "#28374A"
TEXT = "#F6F8FB"
MUTED = "#A7B4C6"
FAINT = "#66758B"
GREEN = "#28D783"
RED = "#F25F5C"

TEAM_COLORS = {
    "ARI": "#A71930",
    "AZ": "#A71930",
    "ATL": "#CE1141",
    "BAL": "#DF4601",
    "BOS": "#BD3039",
    "CHC": "#0E3386",
    "CWS": "#C4CED4",
    "CHW": "#C4CED4",
    "CIN": "#C6011F",
    "CLE": "#E31937",
    "COL": "#C4CED4",
    "DET": "#FA4616",
    "HOU": "#EB6E1F",
    "KC": "#9BB7D4",
    "LAA": "#BA0021",
    "LAD": "#3E8EDE",
    "MIA": "#00A3E0",
    "MIL": "#FFC52F",
    "MIN": "#D31145",
    "NYM": "#FF5910",
    "NYY": "#C4CED4",
    "OAK": "#EFB21E",
    "ATH": "#EFB21E",
    "PHI": "#E81828",
    "PIT": "#FDB827",
    "SD": "#FFC425",
    "SEA": "#00A3AD",
    "SF": "#FD5A1E",
    "STL": "#C41E3A",
    "TB": "#8FBCE6",
    "TEX": "#3D6EB5",
    "TOR": "#4A90E2",
    "WSH": "#AB0003",
}


def render_matchup_card_png(share: dict[str, Any], static_root: Path) -> bytes:
    image = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(image)
    fonts = FontBook()

    draw_soft_background(image)
    draw.rounded_rectangle((42, 38, W - 42, H - 38), radius=30, fill=CARD, outline=(255, 197, 61, 118), width=2)
    draw_header(image, draw, fonts, share, static_root)

    players = share["players"]
    draw_player(image, draw, fonts, players["a"], share, "a", (72, 174, 492, 402))
    draw_tie(draw, fonts, share, (520, 232, 680, 344))
    draw_player(image, draw, fonts, players["b"], share, "b", (708, 174, 1128, 402))
    draw_market(draw, fonts, share, 84, 438, 1032, 18)
    draw_pick_strip(draw, fonts, share)

    out = BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def draw_soft_background(image: Image.Image) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((780, -230, 1350, 300), fill=(255, 197, 61, 18))
    d.ellipse((-260, 400, 370, 900), fill=(46, 217, 138, 13))
    d.ellipse((120, -260, 590, 180), fill=(74, 144, 226, 8))
    d.rounded_rectangle((76, 74, W - 76, H - 74), radius=24, outline=(255, 255, 255, 10), width=1)
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(54)))


def draw_header(image: Image.Image, draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any], static_root: Path) -> None:
    add_logo(image, static_root, (80, 70), 54)
    draw.text((148, 76), "Chalked", font=fonts.brand, fill=TEXT)
    draw.text((148, 118), "Share Snapshot", font=fonts.label, fill=FAINT)

    stat = str(share.get("stat_label") or "Matchup").replace(" - Game", "").upper()
    pill = clamp_text(draw, stat, fonts.pill, 360)
    pill_w = text_width(draw, pill, fonts.pill) + 42
    x2 = W - 76
    draw.rounded_rectangle((x2 - pill_w, 76, x2, 118), radius=21, fill=PANEL, outline=(255, 197, 61, 115), width=1)
    draw.text((x2 - pill_w + 21, 86), pill, font=fonts.pill, fill=GOLD)
    status = clamp_text(draw, status_line(share), fonts.body, 470)
    draw.text((x2 - text_width(draw, status, fonts.body), 128), status, font=fonts.body, fill=MUTED)


def add_logo(image: Image.Image, static_root: Path, xy: tuple[int, int], size: int) -> None:
    logo_path = static_root / "assets" / "chalked-logo-mark.png"
    if not logo_path.exists():
        return
    logo = Image.open(logo_path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    halo = Image.new("RGBA", logo.size, (255, 197, 61, 0))
    halo.putalpha(logo.getchannel("A").filter(ImageFilter.GaussianBlur(10)))
    image.alpha_composite(halo, xy)
    image.alpha_composite(logo, xy)


def draw_player(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: "FontBook",
    player: dict[str, Any],
    share: dict[str, Any],
    side: str,
    box: tuple[int, int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    team = str(player.get("team") or "")
    color = TEAM_COLORS.get(team, GOLD)
    selected = share.get("pick", {}).get("side") == side
    winner = share.get("winner_side") == side
    border = GOLD if selected or winner else LINE

    draw.rounded_rectangle(box, radius=22, fill=PANEL, outline=border, width=2 if selected or winner else 1)
    draw.rounded_rectangle((x1, y1, x2, y1 + 7), radius=22, fill=color)
    draw_headshot(image, draw, fonts, player, color, (x1 + 28, y1 + 34), 96)

    name = clamp_text(draw, str(player.get("name") or "Player"), fonts.player, x2 - x1 - 160)
    draw.text((x1 + 146, y1 + 38), name, font=fonts.player, fill=TEXT)
    opp = f" vs {player.get('opponent')}" if player.get("opponent") else ""
    meta = f"{team} - {player.get('position') or ''}{opp}".strip()
    draw.text((x1 + 146, y1 + 82), clamp_text(draw, meta, fonts.body, x2 - x1 - 170), font=fonts.body, fill=MUTED)

    stat = value_for_side(share, side)
    draw.text((x1 + 30, y1 + 144), stat["label"], font=fonts.label, fill=FAINT)
    draw.text((x1 + 30, y1 + 168), stat["value"], font=fonts.big, fill=TEXT)
    mult = f"{float((share.get('multipliers') or {}).get(side) or 0):.2f}x"
    draw.rounded_rectangle((x2 - 126, y2 - 60, x2 - 30, y2 - 22), radius=12, fill="#101823", outline=(255, 197, 61, 120), width=1)
    draw.text((x2 - 112, y2 - 52), mult, font=fonts.odds, fill=GOLD)


def draw_headshot(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    fonts: "FontBook",
    player: dict[str, Any],
    color: str,
    xy: tuple[int, int],
    size: int,
) -> None:
    x, y = xy
    draw.ellipse((x - 3, y - 3, x + size + 3, y + size + 3), fill=color)
    headshot = fetch_player_headshot(player.get("external_id"), size)
    if headshot:
        image.alpha_composite(headshot, (x, y))
        return
    draw.ellipse((x, y, x + size, y + size), fill=color, outline=(255, 255, 255, 80), width=2)
    initials = "".join(part[:1] for part in str(player.get("name") or "?").split()[:2]).upper()
    tw = text_width(draw, initials, fonts.initials)
    draw.text((x + (size - tw) / 2, y + 31), initials, font=fonts.initials, fill="#071018")


def fetch_player_headshot(external_id: Any, size: int) -> Image.Image | None:
    if not external_id:
        return None
    url = f"https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/{external_id}/headshot/67/current"
    try:
        req = Request(url, headers={"User-Agent": "Chalked/1.0"})
        with urlopen(req, timeout=1.5) as response:
            raw = response.read(600_000)
        shot = Image.open(BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    shot = ImageOps.fit(shot, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.22))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    shot.putalpha(mask)
    return shot


def draw_tie(draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any], box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    selected = share.get("pick", {}).get("side") == "tie"
    winner = share.get("winner_side") == "tie"
    draw.rounded_rectangle(box, radius=18, fill="#0A111A", outline=GOLD if selected or winner else LINE, width=2 if selected or winner else 1)
    draw.text((x1 + 51, y1 + 28), "TIE", font=fonts.tie, fill=GOLD)
    draw.text((x1 + 45, y1 + 64), f"{float((share.get('multipliers') or {}).get('tie') or 0):.2f}x", font=fonts.body, fill=MUTED)


def draw_market(draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any], x: int, y: int, width: int, height: int) -> None:
    market = share.get("market") or {}
    total = max(1, int(market.get("total") or 1))
    a_w = int(width * int(market.get("a") or 0) / total)
    t_w = int(width * int(market.get("tie") or 0) / total)
    b_w = max(0, width - a_w - t_w)
    draw.rounded_rectangle((x, y, x + width, y + height), radius=99, fill="#243246")
    draw.rounded_rectangle((x, y, x + a_w, y + height), radius=99, fill="#D64A40")
    draw.rectangle((x + a_w, y, x + a_w + t_w, y + height), fill=GOLD)
    draw.rounded_rectangle((x + a_w + t_w, y, x + a_w + t_w + b_w, y + height), radius=99, fill=GREEN)

    pct_a = round(100 * int(market.get("a") or 0) / total)
    pct_t = round(100 * int(market.get("tie") or 0) / total)
    pct_b = max(0, 100 - pct_a - pct_t)
    draw.text((x, y + 31), f"{pct_a}% crowd", font=fonts.body, fill=MUTED)
    tie = f"{pct_t}% tie"
    draw.text((x + width // 2 - text_width(draw, tie, fonts.body_b) // 2, y + 31), tie, font=fonts.body_b, fill=GOLD)
    right = f"{pct_b}% crowd"
    draw.text((x + width - text_width(draw, right, fonts.body), y + 31), right, font=fonts.body, fill=MUTED)


def draw_pick_strip(draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any]) -> None:
    pick = share.get("pick")
    draw.rounded_rectangle((72, 520, 1128, 574), radius=18, fill="#090F17", outline=(255, 197, 61, 80), width=1)
    if not pick:
        draw.text((98, 534), "Open this matchup on Chalked and find your edge.", font=fonts.body_b, fill=TEXT)
        draw.text((848, 535), "playchalked.com", font=fonts.body_b, fill=GOLD)
        return
    actor = pick.get("display_name") or pick.get("handle") or "A manager"
    side = pick.get("side_label") or "a side"
    status = pick_status(share, pick)
    status_color = GREEN if status == "CASHED" else GOLD if status == "BAD BEAT" else RED if status == "BUSTED" else MUTED
    draw.text((98, 534), status, font=fonts.body_b, fill=status_color)
    summary = clamp_text(draw, f"{actor} backed {side}", fonts.body_b, 510)
    draw.text((238, 534), summary, font=fonts.body_b, fill=TEXT)
    detail = pick_detail(pick)
    draw.text((1128 - text_width(draw, detail, fonts.body_b) - 26, 534), detail, font=fonts.body_b, fill=GOLD if status != "BUSTED" else RED)


def pick_status(share: dict[str, Any], pick: dict[str, Any]) -> str:
    if pick.get("won"):
        return "CASHED"
    if is_bad_beat(share, pick):
        return "BAD BEAT"
    if pick.get("status") != "settled":
        return "LOCKED"
    return "BUSTED"


def pick_detail(pick: dict[str, Any]) -> str:
    stake = int(pick.get("stake") or 0)
    mult = float(pick.get("mult_at_lock") or 0)
    if pick.get("status") == "settled":
        return str(pick.get("result_label") or f"{stake:,} pts")
    return f"{stake:,} pts @ {mult:.2f}x"


def status_line(share: dict[str, Any]) -> str:
    if share.get("status") == "settled":
        return f"Final: {value_for_side(share, 'a')['value']} to {value_for_side(share, 'b')['value']} {share.get('unit') or ''}"
    if str(share.get("live_state") or "").lower() == "live":
        return f"Live: {share.get('inning') or 'in progress'}"
    return f"Scheduled: {format_start(share.get('game_start'))}"


def value_for_side(share: dict[str, Any], side: str) -> dict[str, str]:
    if share.get("status") == "settled" and share.get(f"actual_{side}") is not None:
        return {"label": "Final", "value": clean_number(share.get(f"actual_{side}") or 0)}
    live = (share.get("live_stats") or {}).get(side) or 0
    if live:
        return {"label": "Live", "value": clean_number(live)}
    return {"label": "Stat", "value": share.get("unit") or "-"}


def format_start(value: Any) -> str:
    if not value:
        return "Start TBA"
    raw = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)
        hour = dt.strftime("%I").lstrip("0") or "0"
        return f"{dt.strftime('%a, %b')} {dt.day}, {hour}:{dt.strftime('%M %p')} UTC"
    except ValueError:
        return str(value)


def is_bad_beat(share: dict[str, Any], pick: dict[str, Any]) -> bool:
    if pick.get("status") != "settled" or pick.get("won"):
        return False
    a = share.get("actual_a")
    b = share.get("actual_b")
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) <= 1.0
    except (TypeError, ValueError):
        return False


def clean_number(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "0"
    return str(int(n)) if n.is_integer() else f"{n:.1f}"


def clamp_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if text_width(draw, text, font) <= max_width:
        return text
    out = text
    while out and text_width(draw, out + "...", font) > max_width:
        out = out[:-1]
    return out.rstrip() + "..."


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


class FontBook:
    def __init__(self) -> None:
        self.brand = load_font(38, bold=True)
        self.player = load_font(30, bold=True)
        self.big = load_font(48, bold=True)
        self.initials = load_font(28, bold=True)
        self.body = load_font(22)
        self.body_b = load_font(23, bold=True)
        self.label = load_font(18, bold=True)
        self.pill = load_font(19, bold=True)
        self.tie = load_font(24, bold=True)
        self.odds = load_font(20, bold=True)


def load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/Inter-Bold.ttf" if bold else "C:/Windows/Fonts/Inter-Regular.ttf",
        "C:/Windows/Fonts/Inter.ttf",
        "/usr/share/fonts/truetype/inter/Inter-Bold.ttf" if bold else "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
        "/usr/share/fonts/opentype/inter/Inter-Bold.otf" if bold else "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
        "/usr/local/share/fonts/Inter-Bold.ttf" if bold else "/usr/local/share/fonts/Inter-Regular.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()
