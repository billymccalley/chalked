from __future__ import annotations

import math
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont


W, H = 1200, 630
GOLD = "#FFC53D"
BG = "#0B0F14"
PANEL = "#141B24"
PANEL2 = "#101721"
LINE = "#29384C"
TEXT = "#F3F6FA"
MUTED = "#9AA8BB"
FAINT = "#647086"
GREEN = "#2ED98A"
LOSS = "#FF6B6B"

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
    add_glow(image, 960, -50, 420, (255, 197, 61), 44)
    add_glow(image, 120, 680, 420, (46, 217, 138), 28)
    add_glow(image, -80, 60, 300, (255, 90, 72), 24)
    draw_contours(draw)

    draw.rounded_rectangle((36, 36, W - 36, H - 36), radius=28, fill=(20, 27, 36, 224), outline=(255, 197, 61, 116), width=2)
    add_logo(image, static_root)

    fonts = FontBook()
    stat = str(share.get("stat_label") or "Matchup").upper()
    draw.rounded_rectangle((66, 68, 360, 108), radius=20, fill=PANEL2, outline=GOLD, width=1)
    draw.text((90, 78), clamp_text(draw, stat, fonts.mono_b, 245), font=fonts.mono_b, fill=GOLD)
    draw.text((855, 80), "CHALKED SHARE SNAPSHOT", font=fonts.mono, fill=FAINT)

    status = status_line(share)
    draw.text((66, 126), status, font=fonts.mono, fill=MUTED)

    players = share["players"]
    draw_player(draw, fonts, players["a"], share, "a", (66, 170, 486, 398))
    draw_tie(draw, fonts, share, (520, 228, 680, 342))
    draw_player(draw, fonts, players["b"], share, "b", (714, 170, 1134, 398))
    draw_market(draw, fonts, share, 78, 430, 1044, 22)
    draw_pick_strip(draw, fonts, share)

    out = BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def add_glow(image: Image.Image, cx: int, cy: int, r: int, rgb: tuple[int, int, int], alpha: int) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*rgb, alpha))
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(r // 2)))


def draw_contours(draw: ImageDraw.ImageDraw) -> None:
    for y in range(-140, H + 180, 78):
        pts = []
        for x in range(-60, W + 90, 70):
            wobble = int(18 * math.sin((x + y) * 0.014))
            pts.append((x, y + wobble))
        draw.line(pts, fill=(255, 255, 255, 8), width=2)


def add_logo(image: Image.Image, static_root: Path) -> None:
    logo_path = static_root / "assets" / "chalked-logo-mark.png"
    if not logo_path.exists():
        return
    logo = Image.open(logo_path).convert("RGBA").resize((92, 92), Image.Resampling.LANCZOS)
    halo = Image.new("RGBA", logo.size, (255, 197, 61, 0))
    halo.putalpha(logo.getchannel("A").filter(ImageFilter.GaussianBlur(14)))
    image.alpha_composite(halo, (1018, 496))
    image.alpha_composite(logo, (1018, 496))


def draw_player(draw: ImageDraw.ImageDraw, fonts: "FontBook", player: dict[str, Any], share: dict[str, Any], side: str, box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    team = str(player.get("team") or "")
    color = TEAM_COLORS.get(team, GOLD)
    selected = share.get("pick", {}).get("side") == side
    winner = share.get("winner_side") == side
    outline = color if selected or winner else LINE
    draw.rounded_rectangle(box, radius=18, fill=PANEL2, outline=outline, width=2)
    initials = "".join(part[:1] for part in str(player.get("name") or "?").split()[:2]).upper()
    draw.ellipse((x1 + 28, y1 + 28, x1 + 102, y1 + 102), fill=color, outline=(255, 255, 255, 60), width=2)
    draw.text((x1 + 50, y1 + 49), initials, font=fonts.name, fill="#0B0F14")
    name = clamp_text(draw, str(player.get("name") or "Player"), fonts.player, x2 - x1 - 150)
    draw.text((x1 + 122, y1 + 34), name, font=fonts.player, fill=color)
    opp = f" vs {player.get('opponent')}" if player.get("opponent") else ""
    draw.text((x1 + 122, y1 + 76), f"{team} - {player.get('position') or ''}{opp}", font=fonts.body, fill=MUTED)
    stat = value_for_side(share, side)
    draw.text((x1 + 34, y1 + 128), stat["label"], font=fonts.mono, fill=FAINT)
    draw.text((x1 + 34, y1 + 154), stat["value"], font=fonts.big, fill=TEXT)
    mult = float((share.get("multipliers") or {}).get(side) or 0)
    draw.rounded_rectangle((x2 - 128, y2 - 58, x2 - 34, y2 - 20), radius=10, fill=(255, 197, 61, 24), outline=(255, 197, 61, 120), width=1)
    draw.text((x2 - 111, y2 - 50), f"{mult:.2f}x", font=fonts.mono_b, fill=GOLD)


def draw_tie(draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any], box: tuple[int, int, int, int]) -> None:
    x1, y1, x2, y2 = box
    selected = share.get("pick", {}).get("side") == "tie"
    winner = share.get("winner_side") == "tie"
    draw.rounded_rectangle(box, radius=16, fill="#0D141E", outline=GOLD if selected or winner else LINE, width=2)
    draw.text((x1 + 48, y1 + 30), "TIE", font=fonts.mono_b, fill=GOLD)
    draw.text((x1 + 45, y1 + 62), f"{float((share.get('multipliers') or {}).get('tie') or 0):.2f}x", font=fonts.mono, fill=MUTED)


def draw_market(draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any], x: int, y: int, width: int, height: int) -> None:
    market = share.get("market") or {}
    total = max(1, int(market.get("total") or 1))
    a = int(width * int(market.get("a") or 0) / total)
    tie = int(width * int(market.get("tie") or 0) / total)
    b = max(0, width - a - tie)
    draw.rounded_rectangle((x, y, x + width, y + height), radius=99, fill="#223044")
    draw.rounded_rectangle((x, y, x + a, y + height), radius=99, fill="#D84A3C")
    draw.rectangle((x + a, y, x + a + tie, y + height), fill=GOLD)
    draw.rounded_rectangle((x + a + tie, y, x + a + tie + b, y + height), radius=99, fill=GREEN)
    pct_a = round(100 * int(market.get("a") or 0) / total)
    pct_t = round(100 * int(market.get("tie") or 0) / total)
    pct_b = max(0, 100 - pct_a - pct_t)
    draw.text((x, y + 34), f"{pct_a}% - {int(market.get('a') or 0):,} pts", font=fonts.mono, fill=MUTED)
    draw.text((x + width // 2 - 72, y + 34), f"{pct_t}% tie - {int(market.get('tie') or 0):,} pts", font=fonts.mono_b, fill=GOLD)
    right = f"{pct_b}% - {int(market.get('b') or 0):,} pts"
    draw.text((x + width - text_width(draw, right, fonts.mono), y + 34), right, font=fonts.mono, fill=MUTED)


def draw_pick_strip(draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any]) -> None:
    pick = share.get("pick")
    y = 522
    if not pick:
        draw.text((66, y), "Open this matchup on Chalked and find your edge.", font=fonts.body_b, fill=TEXT)
        draw.text((66, y + 38), "playchalked.com", font=fonts.mono, fill=GOLD)
        return
    actor = pick.get("display_name") or pick.get("handle") or "A manager"
    side = pick.get("side_label") or "a side"
    result = str(pick.get("result_label") or "")
    status = "CASHED" if pick.get("won") else "BAD BEAT" if is_bad_beat(share, pick) else "LOCKED" if pick.get("status") != "settled" else "BUSTED"
    status_color = GREEN if status == "CASHED" else GOLD if status == "BAD BEAT" else LOSS if status == "BUSTED" else MUTED
    draw.rounded_rectangle((66, 506, 1134, 580), radius=16, fill=(8, 12, 18), outline=(255, 197, 61, 80), width=1)
    draw.text((92, y), status, font=fonts.mono_b, fill=status_color)
    draw.text((230, y), clamp_text(draw, f"{actor} backed {side}", fonts.body_b, 560), font=fonts.body_b, fill=TEXT)
    stake = int(pick.get("stake") or 0)
    mult = float(pick.get("mult_at_lock") or 0)
    detail = f"{stake:,} pts @ {mult:.2f}x"
    if result and pick.get("status") == "settled":
        detail += f" -> {result}"
    draw.text((92, y + 36), clamp_text(draw, detail, fonts.mono_b, 900), font=fonts.mono_b, fill=GOLD if status != "BUSTED" else LOSS)


def status_line(share: dict[str, Any]) -> str:
    if share.get("status") == "settled":
        return f"FINAL - {value_for_side(share, 'a')['value']} to {value_for_side(share, 'b')['value']} {share.get('unit') or ''}"
    live_state = str(share.get("live_state") or "").lower()
    if live_state == "live":
        inning = share.get("inning") or "Live"
        return f"LIVE - {inning}"
    start = format_start(share.get("game_start"))
    return f"{share.get('game_status') or 'Scheduled'} - {start}"


def value_for_side(share: dict[str, Any], side: str) -> dict[str, str]:
    if share.get("status") == "settled" and share.get(f"actual_{side}") is not None:
        value = share.get(f"actual_{side}") or 0
        return {"label": "Final stat", "value": clean_number(value)}
    live = (share.get("live_stats") or {}).get(side) or 0
    if live:
        return {"label": "Live stat", "value": clean_number(live)}
    return {"label": "Pick stat", "value": share.get("unit") or "-"}


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
        self.title = load_font(58, bold=True)
        self.player = load_font(34, bold=True)
        self.name = load_font(24, bold=True)
        self.big = load_font(46, bold=True)
        self.body = load_font(23)
        self.body_b = load_font(24, bold=True)
        self.mono = load_font(21, mono=True)
        self.mono_b = load_font(22, bold=True, mono=True)


def load_font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.ImageFont:
    candidates = []
    if mono:
        candidates.extend(
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
            ]
        )
    candidates.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        ]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()
