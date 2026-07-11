from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


W, H = 1200, 630
GOLD = "#FFC53D"
BG = "#0B0F14"
CARD = "#121A24"
PANEL = "#0F151D"
LINE = "#243040"
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
    fonts = FontBook(static_root)

    draw_soft_background(image)
    draw.rounded_rectangle((42, 38, W - 42, H - 38), radius=30, fill=CARD, outline=(255, 197, 61, 54), width=1)
    draw_header(image, draw, fonts, share, static_root)

    players = share["players"]
    draw_player(image, draw, fonts, players["a"], share, "a", (72, 178, 506, 356))
    draw_tie(draw, fonts, share, (532, 229, 668, 306))
    draw_player(image, draw, fonts, players["b"], share, "b", (694, 178, 1128, 356))
    draw_market(image, draw, fonts, share, 84, 414, 1032, 18)
    draw_pick_strip(draw, fonts, share)

    out = BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_slate_results_card_png(share: dict[str, Any], static_root: Path) -> bytes:
    image = Image.new("RGBA", (W, H), BG)
    draw = ImageDraw.Draw(image)
    fonts = FontBook(static_root)

    draw_soft_background(image)
    draw.rounded_rectangle((42, 38, W - 42, H - 38), radius=30, fill=CARD, outline=(255, 197, 61, 34), width=1)
    add_logo(image, static_root, (80, 70), 54)
    draw.text((148, 76), "Chalked", font=fonts.brand, fill=TEXT)
    draw.text((148, 118), "Slate Results", font=fonts.label, fill=FAINT)

    label = str(share.get("slate_label") or "Slate").upper()
    pill_w = text_width(draw, label, fonts.pill) + 42
    draw.rounded_rectangle((W - 76 - pill_w, 76, W - 76, 118), radius=21, fill="#101823")
    draw.text((W - 76 - pill_w + 21, 86), label, font=fonts.pill, fill=GOLD)
    rank = str(share.get("rank_label") or "")
    if rank:
        rank_text = f"Rank {rank} today"
        draw.text((W - 76 - text_width(draw, rank_text, fonts.body), 128), rank_text, font=fonts.body, fill=MUTED)

    manager = clamp_text(draw, str(share.get("manager") or "Manager"), fonts.player, 520)
    league = clamp_text(draw, str(share.get("league_name") or "Chalked"), fonts.body, 520)
    draw.text((78, 152), manager, font=fonts.player, fill=TEXT)
    draw.text((78, 191), league, font=fonts.body, fill=MUTED)

    net = int(share.get("net") or 0)
    net_text = signed_points(net)
    net_color = GREEN if net >= 0 else RED
    draw.text((W - 78 - text_width(draw, net_text, fonts.result_hero), 154), net_text, font=fonts.result_hero, fill=net_color)
    draw.text((W - 78 - text_width(draw, "net this slate", fonts.label), 216), "net this slate", font=fonts.label, fill=FAINT)

    rows = list(share.get("rows") or [])
    wins = sum(1 for row in rows if row.get("won"))
    losses = max(0, len(rows) - wins)
    best = max([int(row.get("payout") or 0) for row in rows] or [0])
    worst = min([int(row.get("payout") or 0) for row in rows] or [0])
    chip_specs = [
        (78, 246, "Record", f"{wins}-{losses}", GOLD),
        (314, 246, "Best hit", signed_points(best), GREEN if best >= 0 else soft_red()),
        (550, 246, "Worst miss", signed_points(worst), soft_red() if worst < 0 else MUTED),
        (786, 246, "Daily rank", str(share.get("rank_label") or "-"), GOLD),
    ]
    for x, y, label, value, color in chip_specs:
        draw_stat_chip(draw, fonts, x, y, label, value, color)

    draw.text((78, 325), "Biggest swings", font=fonts.label, fill=FAINT)
    highlights = sorted(rows, key=lambda row: abs(int(row.get("payout") or 0)), reverse=True)[:3]
    y = 348
    if not highlights:
        draw.rounded_rectangle((78, y, W - 78, y + 78), radius=18, fill=PANEL, outline=LINE, width=1)
        draw.text((108, y + 26), "No picks on this slate.", font=fonts.body_b, fill=MUTED)
    for row in highlights:
        draw_result_row(draw, fonts, row, y)
        y += 62
    hidden = max(0, len(rows) - len(highlights))
    if hidden:
        more = f"+ {hidden} more pick{'s' if hidden != 1 else ''} on the full slate"
        draw.text((78, min(y + 2, 540)), more, font=fonts.body, fill=FAINT)

    site = "playchalked.com"
    draw.text((W - 78 - text_width(draw, site, fonts.body_b), 558), site, font=fonts.body_b, fill=GOLD)

    out = BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def draw_result_row(draw: ImageDraw.ImageDraw, fonts: "FontBook", row: dict[str, Any], y: int) -> None:
    won = bool(row.get("won"))
    color = GREEN if won else soft_red()
    status = "WIN" if won else "LOSS"
    draw.rounded_rectangle((78, y, W - 78, y + 52), radius=16, fill="#0F1721", outline="#263445", width=1)
    draw.rounded_rectangle((78, y, 84, y + 52), radius=3, fill=color)
    draw.rounded_rectangle((98, y + 15, 156, y + 37), radius=11, fill=blend_hex(color, BG, 0.76))
    draw.text((127 - text_width(draw, status, fonts.result_badge) / 2, y + 20), status, font=fonts.result_badge, fill=color)

    side = str(row.get("side_label") or "Pick")
    title = clamp_text(draw, side, fonts.result_row, 560)
    draw.text((176, y + 9), title, font=fonts.result_row, fill=TEXT)
    sub = f"{int(row.get('stake') or 0):,} @ {float(row.get('mult_at_lock') or 0):.2f}x"
    if row.get("actual_a") is not None and row.get("actual_b") is not None:
        sub += f" - {clean_number(row.get('actual_a'))} to {clean_number(row.get('actual_b'))} {row.get('unit') or ''}"
    draw.text((176, y + 33), clamp_text(draw, sub, fonts.result_sub, 560), font=fonts.result_sub, fill=FAINT)

    payout = int(row.get("payout") or 0)
    value = signed_points(payout)
    draw.text((W - 102 - text_width(draw, value, fonts.result_row), y + 16), value, font=fonts.result_row, fill=GREEN if payout >= 0 else soft_red())


def draw_stat_chip(draw: ImageDraw.ImageDraw, fonts: "FontBook", x: int, y: int, label: str, value: str, color: str) -> None:
    draw.rounded_rectangle((x, y, x + 214, y + 50), radius=14, fill="#0F1721", outline="#263445", width=1)
    draw.text((x + 16, y + 9), label.upper(), font=fonts.result_sub, fill=FAINT)
    draw.text((x + 16, y + 27), value, font=fonts.body_b, fill=color)


def draw_soft_background(image: Image.Image) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse((800, -240, 1360, 280), fill=(255, 197, 61, 12))
    d.ellipse((-260, 410, 360, 890), fill=(46, 217, 138, 10))
    d.ellipse((120, -260, 590, 180), fill=(74, 144, 226, 8))
    d.rounded_rectangle((76, 74, W - 76, H - 74), radius=24, outline=(255, 255, 255, 8), width=1)
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(54)))


def draw_header(image: Image.Image, draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any], static_root: Path) -> None:
    add_logo(image, static_root, (80, 70), 54)
    draw.text((148, 76), "Chalked", font=fonts.brand, fill=TEXT)
    draw.text((148, 118), "Share Snapshot", font=fonts.label, fill=FAINT)

    stat = str(share.get("stat_label") or "Matchup").replace(" - Game", "").upper()
    pill = clamp_text(draw, stat, fonts.pill, 360)
    pill_w = text_width(draw, pill, fonts.pill) + 42
    x2 = W - 76
    draw.rounded_rectangle((x2 - pill_w, 76, x2, 118), radius=21, fill="#101823")
    draw.text((x2 - pill_w + 21, 86), pill, font=fonts.pill, fill=GOLD)
    status = clamp_text(draw, status_line(share), fonts.body, 470)
    draw.text((x2 - text_width(draw, status, fonts.body), 128), status, font=fonts.body, fill=MUTED)


def add_logo(image: Image.Image, static_root: Path, xy: tuple[int, int], size: int) -> None:
    logo_path = static_root / "assets" / "chalked-logo-mark.png"
    if not logo_path.exists():
        return
    logo = Image.open(logo_path).convert("RGBA")
    logo = remove_dark_backdrop(logo).resize((size, size), Image.Resampling.LANCZOS)
    shadow = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    shadow.putalpha(logo.getchannel("A").filter(ImageFilter.GaussianBlur(5)))
    image.alpha_composite(shadow, (xy[0] + 1, xy[1] + 3))
    image.alpha_composite(logo, xy)


def remove_dark_backdrop(image: Image.Image) -> Image.Image:
    pixels = []
    for r, g, b, a in image.getdata():
        if r < 12 and g < 12 and b < 12:
            pixels.append((r, g, b, 0))
        else:
            pixels.append((r, g, b, a))
    image.putdata(pixels)
    return image


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
    selected = (share.get("pick") or {}).get("side") == side
    winner = share.get("winner_side") == side
    border = GOLD if selected or winner else LINE

    draw.rounded_rectangle(box, radius=22, fill=PANEL, outline=border, width=2 if selected or winner else 1)
    draw_headshot(image, draw, fonts, player, color, (x1 + 28, y1 + 33), 76)

    name = clamp_text(draw, str(player.get("name") or "Player"), fonts.player, x2 - x1 - 136)
    draw.text((x1 + 120, y1 + 36), name, font=fonts.player, fill=TEXT)
    opp = f" vs {player.get('opponent')}" if player.get("opponent") else ""
    meta = f"{team} - {player.get('position') or ''}{opp}".strip()
    draw.text((x1 + 120, y1 + 74), clamp_text(draw, meta, fonts.body, x2 - x1 - 144), font=fonts.body, fill=MUTED)

    stat = value_for_side(share, side)
    draw.text((x1 + 30, y2 - 59), stat["value"], font=fonts.big, fill=TEXT)
    mult = f"{float((share.get('multipliers') or {}).get(side) or 0):.2f}x"
    odds_w = text_width(draw, mult, fonts.odds) + 30
    draw.rounded_rectangle((x2 - odds_w - 30, y2 - 56, x2 - 30, y2 - 22), radius=11, fill="#101823", outline=(255, 197, 61, 120), width=1)
    draw.text((x2 - odds_w - 15, y2 - 49), mult, font=fonts.odds, fill=GOLD)


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
    selected = (share.get("pick") or {}).get("side") == "tie"
    winner = share.get("winner_side") == "tie"
    draw.text((x1 + 52, y1 - 30), "VS", font=fonts.label, fill=FAINT)
    draw.rounded_rectangle(box, radius=18, fill="#0A111A", outline=GOLD if selected or winner else LINE, width=2 if selected or winner else 1)
    draw.text((x1 + 47, y1 + 15), "TIE", font=fonts.tie, fill=GOLD)
    mult = f"{float((share.get('multipliers') or {}).get('tie') or 0):.2f}x"
    draw.text((x1 + (x2 - x1 - text_width(draw, mult, fonts.body)) / 2, y1 + 43), mult, font=fonts.body, fill=MUTED)


def draw_market(image: Image.Image, draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any], x: int, y: int, width: int, height: int) -> None:
    market = share.get("market") or {}
    total = max(1, int(market.get("total") or 1))
    a_w = int(width * int(market.get("a") or 0) / total)
    t_w = int(width * int(market.get("tie") or 0) / total)
    b_w = max(0, width - a_w - t_w)
    color_a = readable_color(TEAM_COLORS.get((share.get("players") or {}).get("a", {}).get("team"), RED))
    color_b = readable_color(TEAM_COLORS.get((share.get("players") or {}).get("b", {}).get("team"), GREEN))
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    layer_draw = ImageDraw.Draw(layer)
    layer_draw.rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill="#243246")
    draw_gradient(layer, x, y, max(1, a_w), height, color_a, blend_hex(color_a, "#FFFFFF", 0.08))
    draw_gradient(layer, x + a_w + t_w, y, max(1, b_w), height, blend_hex(color_b, "#FFFFFF", 0.08), color_b)
    if t_w > 0:
        layer_draw.rectangle((x + a_w, y, x + a_w + t_w, y + height), fill=GOLD)
        divider = (10, 15, 22, 72)
        layer_draw.rectangle((x + a_w - 1, y, x + a_w + 1, y + height), fill=divider)
        layer_draw.rectangle((x + a_w + t_w - 1, y, x + a_w + t_w + 1, y + height), fill=divider)
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((x, y, x + width, y + height), radius=height // 2, fill=255)
    layer.putalpha(mask)
    image.alpha_composite(layer)

    pct_a = round(100 * int(market.get("a") or 0) / total)
    pct_t = round(100 * int(market.get("tie") or 0) / total)
    pct_b = max(0, 100 - pct_a - pct_t)
    draw.text((x, y + 31), f"{pct_a}% crowd", font=fonts.body, fill=MUTED)
    tie = f"{pct_t}% tie"
    draw.text((x + width // 2 - text_width(draw, tie, fonts.body_b) // 2, y + 31), tie, font=fonts.body_b, fill=GOLD)
    right = f"{pct_b}% crowd"
    draw.text((x + width - text_width(draw, right, fonts.body), y + 31), right, font=fonts.body, fill=MUTED)


def draw_gradient(image: Image.Image, x: int, y: int, width: int, height: int, left: str, right: str) -> None:
    if width <= 0:
        return
    d = ImageDraw.Draw(image)
    lrgb = hex_to_rgb(left)
    rrgb = hex_to_rgb(right)
    for i in range(width):
        t = 0 if width == 1 else i / (width - 1)
        color = tuple(round(lrgb[j] + (rrgb[j] - lrgb[j]) * t) for j in range(3))
        d.line((x + i, y, x + i, y + height), fill=(*color, 255))


def readable_color(hex_color: str) -> str:
    rgb = hex_to_rgb(hex_color)
    if luminance(rgb) < 0.13:
        rgb = blend_rgb(rgb, (255, 255, 255), 0.34)
    return rgb_to_hex(rgb)


def soft_red() -> str:
    return "#FF6B6B"


def blend_hex(left: str, right: str, amount: float) -> str:
    return rgb_to_hex(blend_rgb(hex_to_rgb(left), hex_to_rgb(right), amount))


def blend_rgb(left: tuple[int, int, int], right: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(round(left[i] + (right[i] - left[i]) * amount) for i in range(3))


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    clean = str(hex_color or GOLD).lstrip("#")
    if len(clean) == 3:
        clean = "".join(ch * 2 for ch in clean)
    try:
        n = int(clean[:6], 16)
    except ValueError:
        return (255, 197, 61)
    return ((n >> 16) & 255, (n >> 8) & 255, n & 255)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def luminance(rgb: tuple[int, int, int]) -> float:
    values = []
    for channel in rgb:
        c = channel / 255
        values.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]


def draw_pick_strip(draw: ImageDraw.ImageDraw, fonts: "FontBook", share: dict[str, Any]) -> None:
    pick = share.get("pick")
    draw.rounded_rectangle((72, 520, 1128, 574), radius=18, fill="#090F17", outline=(255, 197, 61, 80), width=1)
    if not pick:
        draw.text((98, 534), "OPEN MATCHUP", font=fonts.body_b, fill=GOLD)
        draw.text((298, 534), "Pick a player, call the tie, or fade the crowd.", font=fonts.body_b, fill=TEXT)
        draw.text((1128 - text_width(draw, "playchalked.com", fonts.body_b) - 26, 534), "playchalked.com", font=fonts.body_b, fill=MUTED)
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
        return "PENDING"
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


def signed_points(value: int | float) -> str:
    n = int(round(float(value or 0)))
    return f"+{n:,}" if n > 0 else f"{n:,}"


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
    def __init__(self, static_root: Path) -> None:
        font_dir = static_root / "assets" / "fonts"
        self.brand = load_font(38, black=True, font_dir=font_dir)
        self.player = load_font(29, bold=True, font_dir=font_dir)
        self.big = load_font(43, black=True, font_dir=font_dir)
        self.initials = load_font(26, bold=True, font_dir=font_dir)
        self.body = load_font(22, font_dir=font_dir)
        self.body_b = load_font(23, bold=True, font_dir=font_dir)
        self.label = load_font(18, bold=True, font_dir=font_dir)
        self.pill = load_font(19, bold=True, font_dir=font_dir)
        self.tie = load_font(24, black=True, font_dir=font_dir)
        self.odds = load_font(20, bold=True, font_dir=font_dir)
        self.result_hero = load_font(58, black=True, font_dir=font_dir)
        self.result_big = load_font(46, black=True, font_dir=font_dir)
        self.result_row = load_font(20, bold=True, font_dir=font_dir)
        self.result_sub = load_font(15, font_dir=font_dir)
        self.result_badge = load_font(13, bold=True, font_dir=font_dir)


def load_font(size: int, bold: bool = False, black: bool = False, font_dir: Path | None = None) -> ImageFont.ImageFont:
    local = []
    if font_dir:
        weight = "Black" if black else "Bold" if bold else "Regular"
        local = [
            font_dir / f"Inter-{weight}.ttf",
            font_dir / f"Inter-{weight}.otf",
            font_dir / ("Inter-Bold.ttf" if bold or black else "Inter-Regular.ttf"),
        ]
    win_weight = "Black" if black else "Bold" if bold else "Regular"
    candidates = [
        *[str(path) for path in local],
        f"C:/Windows/Fonts/Inter-{win_weight}.ttf",
        "C:/Windows/Fonts/Inter.ttf",
        f"/usr/share/fonts/truetype/inter/Inter-{win_weight}.ttf",
        f"/usr/share/fonts/opentype/inter/Inter-{win_weight}.otf",
        f"/usr/local/share/fonts/Inter-{win_weight}.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold or black else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold or black else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()
