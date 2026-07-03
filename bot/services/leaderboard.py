"""services/leaderboard.py — Lógica de rankings y construcción de embeds."""
from datetime import datetime, timezone, timedelta

import discord

from db import matches as db_matches

TZ_UY = timezone(timedelta(hours=-3))

CATEGORIES = [
    ("Kills",    "total_kills",    0xED4245, "💀", "Kills"),
    ("K/D",      "kd_ratio",       0xF1C40F, "⚔️", "K/D"),
    ("Partidas", "matches_played", 0x5865F2, "🎮", "Partidas"),
    ("Combat",   "total_combat",   0xEB459E, "🔥", "Combat"),
    ("Offense",  "total_offense",  0xE67E22, "⚔️", "Offense"),
    ("Defense",  "total_defense",  0x57F287, "🛡️", "Defense"),
    ("Support",  "total_support",  0x1ABC9C, "🤝", "Support"),
]

CATEGORY_BY_COLUMN = {
    col: (name, color, icon, value_label)
    for name, col, color, icon, value_label in CATEGORIES
}

SNAPSHOT_CATEGORIES = [c for c in CATEGORIES if c[1] != "kd_ratio"]

PERIOD_LABELS = {
    "all":   "Histórico",
    "day":   "Día",
    "week":  "Semana",
    "month": "Mes",
}

FOOTER_PERIOD_TEXT = {
    "day":   "del día",
    "week":  "de la semana",
    "month": "del mes",
}

HLL_WEAPONS = [
    "M1A1 THOMPSON", "M3 GREASE GUN", "MP40", "PPSH 41", "PPSH 41 W/DRUM",
    "Sten Gun Mk.II", "Sten Gun Mk.V", "Lanchester", "M1928A1 THOMPSON",
    "M1 GARAND", "M1 CARBINE", "GEWEHR 43", "SVT40",
    "KARABINER 98K", "MOSIN NAGANT 1891", "MOSIN NAGANT 91/30", "MOSIN NAGANT M38",
    "SMLE No.1 Mk III", "Rifle No.4 Mk I", "Rifle No.5 Mk I",
    "M1918A2 BAR", "STG44", "FG42", "Bren Gun",
    "M97 TRENCH GUN",
    "BROWNING M1919", "MG34", "MG42", "DP-27", "Lewis Gun",
    "M1903 SPRINGFIELD", "KARABINER 98K x8", "FG42 x4",
    "SCOPED MOSIN NAGANT 91/30", "SCOPED SVT40",
    "Lee-Enfield Pattern 1914 Sniper", "Rifle No.4 Mk I Sniper",
    "COLT M1911", "WALTHER P38", "LUGER P08", "NAGANT M1895", "TOKAREV TT33", "Webley MK VI",
    "M2 FLAMETHROWER", "FLAMMENWERFER 41", "FLAMETHROWER",
    "M3 KNIFE", "FELDSPATEN", "MPL-50 SPADE", "Fairbairn\u2013Sykes",
    "MK2 GRENADE", "M24 STIELHANDGRANATE", "M43 STIELHANDGRANATE",
    "RG-42 GRENADE", "MOLOTOV", "Mills Bomb", "No.82 Grenade",
    "SATCHEL", "SATCHEL CHARGE",
    "M2 AP MINE", "S-MINE", "POMZ AP MINE", "A.P. Shrapnel Mine Mk II",
    "M1A1 AT MINE", "TELLERMINE 43", "TM-35 AT MINE", "A.T. Mine G.S. Mk V",
    "BAZOOKA", "PANZERSCHRECK", "PTRS-41", "PIAT", "Boys Anti-tank Rifle",
    "FLARE GUN", "No.2 Mk 5 Flare Pistol",
    "155MM HOWITZER [M114]", "150MM HOWITZER [sFH 18]", "122MM HOWITZER [M1938 (M-30)]",
    "QF 25-POUNDER [QF 25-Pounder]", "57MM CANNON [M1 57mm]", "75MM CANNON [PAK 40]",
    "57MM CANNON [ZiS-2]", "QF 6-POUNDER [QF 6-Pounder]",
    "BOMBING RUN", "STRAFING RUN", "PRECISION STRIKE",
]


def steam_profile_link(name: str, steam_id: str) -> str:
    safe = (name or "?").replace("[", "(").replace("]", ")")
    if len(safe) > 22:
        safe = safe[:21] + "…"
    return f"[{safe}](https://steamcommunity.com/profiles/{steam_id})"


def period_start(period_value: str, now: datetime = None) -> datetime:
    now = now or datetime.now(TZ_UY)
    if period_value == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_value == "week":
        return (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if period_value == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now


def period_end(period_value: str, now: datetime = None) -> datetime:
    start = period_start(period_value, now)
    if period_value == "day":
        return start + timedelta(days=1)
    if period_value == "week":
        return start + timedelta(days=7)
    if period_value == "month":
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    return start


async def fetch_leaderboard(pool, col: str, period_value: str,
                             limit: int, reference_date: datetime = None) -> list:
    desde = period_start(period_value, reference_date) if period_value != "all" else None
    hasta = period_end(period_value, reference_date) if period_value != "all" else None
    async with pool.acquire() as conn:
        return await db_matches.fetch_leaderboard(conn, col, period_value, limit, desde, hasta)


async def get_player_ranks(pool, steam_id: str) -> dict:
    rankable_columns = [col for _, col, *_ in CATEGORIES] + ["total_time_seconds"]
    ranks = {}
    async with pool.acquire() as conn:
        for col in rankable_columns:
            ranks[col] = await db_matches.get_player_rank(conn, steam_id, col)
    return ranks


async def get_top_killers_by_weapon(pool, weapon: str, limit: int = 10) -> list:
    async with pool.acquire() as conn:
        return await db_matches.get_top_killers_by_weapon(conn, weapon, limit)


async def get_all_weapons_with_rank(pool, steam_id: str) -> list:
    async with pool.acquire() as conn:
        return await db_matches.get_all_weapons_with_rank(conn, steam_id)


def build_leaderboard_embed(rows, col: str, categoria_name: str, period_value: str,
                             limit: int, now_uy: datetime = None,
                             include_links: bool = True,
                             reference_date: datetime = None) -> discord.Embed:
    _, color, icon, value_label = CATEGORY_BY_COLUMN.get(
        col, (categoria_name, 0xF1C40F, "🏆", categoria_name)
    )
    period_label = PERIOD_LABELS.get(period_value, "Histórico")

    def fmt_partidas(r): return f"{r['matches_played']} partidas"
    def fmt_kd(r):
        kd_val = r["kd_ratio"]
        return f"KD {kd_val:.2f}" if kd_val is not None else "KD —"
    def fmt_deaths(r): return f"{r['total_deaths']} deaths"
    def fmt_horas(r):
        return f"{round((r['total_time_seconds'] or 0) / 3600, 1)}h"

    EXTRAS_BY_COLUMN = {
        "total_kills":    [("Partidas", fmt_partidas), ("Deaths", fmt_deaths)],
        "kd_ratio":       [("Partidas", fmt_partidas)],
        "matches_played": [("Horas", fmt_horas)],
        "total_combat":   [("Partidas", fmt_partidas)],
        "total_offense":  [("Partidas", fmt_partidas)],
        "total_defense":  [("Partidas", fmt_partidas)],
        "total_support":  [("Partidas", fmt_partidas)],
    }
    extras_def = EXTRAS_BY_COLUMN.get(col, [("Partidas", fmt_partidas), ("KD", fmt_kd)])

    lines = []
    for i, r in enumerate(rows):
        pos = i + 1
        if include_links:
            name_display = steam_profile_link(r["last_name"], r["steam_id"])
        else:
            raw_name = (r["last_name"] or "?").replace("[", "(").replace("]", ")")
            name_display = raw_name if len(raw_name) <= 22 else raw_name[:21] + "…"

        value = r[col]
        value_str = f"{value:.2f}" if isinstance(value, float) else str(value)
        extras = [fn(r) for _, fn in extras_def]
        extra_str = (" · " + " · ".join(extras)) if extras else ""
        lines.append(f"{pos}. {name_display} — **{value_str}**{extra_str}")

    embed = discord.Embed(
        title=f"{icon} Top {limit} — {categoria_name} ({period_label})",
        description="\n".join(lines),
        color=color,
    )

    if period_value == "all":
        footer_txt = "📊 Stats históricos acumulados • actualizado cada 30 min"
    else:
        desde = period_start(period_value, reference_date)
        hasta_exclusive = period_end(period_value, reference_date)
        now_uy = now_uy or datetime.now(TZ_UY)
        if reference_date is not None:
            hasta_mostrar = min(hasta_exclusive, now_uy)
            rango = f"Desde: {desde.strftime('%d/%m %H:%M:%S')} — Hasta: {hasta_mostrar.strftime('%d/%m %H:%M:%S')}"
            footer_txt = f"📊 Stats {FOOTER_PERIOD_TEXT[period_value]} ({desde.strftime('%d/%m/%Y')})\n{rango}"
        else:
            rango = f"Desde: {desde.strftime('%d/%m %H:%M:%S')} — Hasta: {now_uy.strftime('%d/%m %H:%M:%S')}"
            footer_txt = f"📊 Stats {FOOTER_PERIOD_TEXT[period_value]} • calculado en vivo\n{rango}"

    embed.set_footer(text=footer_txt)
    return embed


async def build_all_category_embeds(pool, period_value: str, limit: int,
                                     now_uy: datetime = None,
                                     include_links: bool = True,
                                     reference_date: datetime = None) -> list:
    embeds = []
    for name, col, *_ in SNAPSHOT_CATEGORIES:
        rows = await fetch_leaderboard(pool, col, period_value, limit, reference_date)
        if not rows:
            continue
        embeds.append(build_leaderboard_embed(
            rows, col, name, period_value, limit, now_uy, include_links, reference_date
        ))
    return embeds