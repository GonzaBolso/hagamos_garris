"""
leaderboards.py
Lógica compartida para armar rankings de jugadores (queries + embeds),
usada tanto por el comando /hll top como por la tarea automática de
snapshots diarios/semanales/mensuales.
"""
from datetime import datetime, timezone, timedelta

import discord

TZ_UY = timezone(timedelta(hours=-3))

# Categorías disponibles. El orden de esta lista define el orden en que
# se mandan los embeds en el snapshot automático.
CATEGORIES = [
    # (choice_name, column,           color,     icon, value_label)
    ("Kills",    "total_kills",    0xED4245, "💀", "Kills"),
    ("K/D",      "kd_ratio",       0xF1C40F, "⚔️", "K/D"),
    ("Partidas", "matches_played", 0x5865F2, "🎮", "Partidas"),
    ("Combat",   "total_combat",   0xEB459E, "🔥", "Combat"),
    ("Offense",  "total_offense",  0xE67E22, "⚔️", "Offense"),
    ("Defense",  "total_defense",  0x57F287, "🛡️", "Defense"),
    ("Support",  "total_support",  0x1ABC9C, "🤝", "Support"),
]

CATEGORY_BY_COLUMN = {col: (name, color, icon, value_label) for name, col, color, icon, value_label in CATEGORIES}

PERIOD_LABELS = {
    "all":   "Histórico",
    "day":   "Día",
    "week":  "Semana",
    "month": "Mes",
}

FOOTER_PERIOD_TEXT = {
    "day":   "del último día",
    "week":  "de la última semana",
    "month": "del último mes",
}


def steam_profile_link(name: str, steam_id: str) -> str:
    """
    Devuelve el nombre del jugador como link a su perfil de Steam si el steam_id
    tiene formato válido (17 dígitos numéricos). Si es un ID de consola u otro
    formato, devuelve el nombre sin link.
    """
    safe_name = (name or "?").replace("[", "(").replace("]", ")")
    if steam_id and steam_id.isdigit() and len(steam_id) == 17:
        return f"[{safe_name}](https://steamcommunity.com/profiles/{steam_id})"
    return safe_name


def period_start(period_value: str, now: datetime = None) -> datetime:
    """
    Calcula el inicio del período de calendario en hora UY (UTC-3 fijo),
    con el mismo criterio que el DATE_TRUNC usado en el SQL:
      - day:   hoy 00:00
      - week:  lunes de esta semana, 00:00
      - month: día 1 de este mes, 00:00
    """
    now = (now or datetime.now(TZ_UY)).astimezone(TZ_UY)
    if period_value == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_value == "week":
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_of_day - timedelta(days=now.weekday())  # weekday(): lunes=0
    if period_value == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return now


async def fetch_leaderboard(pool, col: str, period_value: str, limit: int):
    """
    Devuelve las filas del ranking para una columna/categoría y período dados.
    period_value: 'all' | 'day' | 'week' | 'month'
    """
    if period_value == "all":
        async with pool.acquire() as conn:
            return await conn.fetch(
                f"""
                SELECT steam_id, last_name, {col}, matches_played,
                       total_kills, total_deaths, kd_ratio
                FROM player_totals
                ORDER BY {col} DESC NULLS LAST
                LIMIT $1
                """,
                limit
            )

    # Períodos de CALENDARIO (no ventana móvil), calculados en hora UY (UTC-3):
    #   día   -> desde las 00:00 de hoy (hora UY)
    #   semana-> desde el lunes 00:00 de esta semana (hora UY)
    #   mes   -> desde el día 1, 00:00 de este mes (hora UY)
    trunc_map = {"day": "day", "week": "week", "month": "month"}
    trunc_unit = trunc_map[period_value]

    async with pool.acquire() as conn:
        return await conn.fetch(
            f"""
            SELECT
                mps.steam_id,
                MAX(mps.player_name)                                AS last_name,
                COUNT(DISTINCT mps.match_id)                        AS matches_played,
                SUM(mps.kills)                                      AS total_kills,
                SUM(mps.deaths)                                     AS total_deaths,
                CASE WHEN SUM(mps.deaths) = 0
                     THEN SUM(mps.kills)::FLOAT
                     ELSE ROUND((SUM(mps.kills)::NUMERIC / SUM(mps.deaths)), 2)
                END                                                  AS kd_ratio,
                SUM(mps.combat_score)                               AS total_combat,
                SUM(mps.offense_score)                              AS total_offense,
                SUM(mps.defense_score)                              AS total_defense,
                SUM(mps.support_score)                              AS total_support
            FROM match_player_stats mps
            JOIN matches m ON m.match_id = mps.match_id
            WHERE m.start_time >= (
                DATE_TRUNC('{trunc_unit}', NOW() AT TIME ZONE 'America/Montevideo')
                AT TIME ZONE 'America/Montevideo'
            )
            GROUP BY mps.steam_id
            ORDER BY {col} DESC NULLS LAST
            LIMIT $1
            """,
            limit
        )


def build_leaderboard_embed(rows, col: str, categoria_name: str, period_value: str,
                             limit: int, now_uy: datetime = None,
                             include_links: bool = True) -> discord.Embed:
    """
    Construye el embed de un ranking ya consultado (rows de fetch_leaderboard).
    include_links=False usa texto plano sin link a Steam (más corto): se usa
    en el snapshot automático, donde 7 embeds van en un solo mensaje y Discord
    limita el total combinado a 6000 caracteres.
    """
    _, color, icon, value_label = CATEGORY_BY_COLUMN.get(col, (categoria_name, 0xF1C40F, "🏆", categoria_name))
    period_label = PERIOD_LABELS.get(period_value, "Histórico")

    header = f"`#   Jugador               {value_label:<8} Partidas  KD`\n" + "─" * 46
    lines = [header]

    for i, r in enumerate(rows):
        rank = f"{i+1:>2}."

        if include_links:
            name_display = steam_profile_link(r["last_name"], r["steam_id"])
        else:
            raw_name = (r["last_name"] or "?").replace("[", "(").replace("]", ")")
            # Salvaguarda: un nombre inusualmente largo no debe poder
            # empujar el total del mensaje sobre el límite de Discord.
            name_display = raw_name if len(raw_name) <= 18 else raw_name[:17] + "…"

        value = r[col]
        value_str = f"{value:.2f}" if isinstance(value, float) else str(value)

        kd_val = r["kd_ratio"]
        kd_str = f"{kd_val:.2f}" if kd_val is not None else "—"

        lines.append(
            f"`{rank}` {name_display} — **{value_str}** "
            f"· {r['matches_played']} partidas · KD {kd_str}"
        )

    embed = discord.Embed(
        title=f"{icon} Top {limit} — {categoria_name} ({period_label})",
        description="\n".join(lines),
        color=color
    )

    if period_value == "all":
        footer_txt = "📊 Stats históricos acumulados • actualizado cada 30 min"
    else:
        now_uy = now_uy or datetime.now(TZ_UY)
        desde = period_start(period_value, now_uy)
        rango = f"Desde: {desde.strftime('%d/%m %H:%M:%S')} — Hasta: {now_uy.strftime('%d/%m %H:%M:%S')}"
        footer_txt = f"📊 Stats {FOOTER_PERIOD_TEXT[period_value]} • calculado en vivo\n{rango}"

    embed.set_footer(text=footer_txt)
    return embed


async def build_all_category_embeds(pool, period_value: str, limit: int, now_uy: datetime = None,
                                      include_links: bool = True):
    """
    Corre fetch_leaderboard + build_leaderboard_embed para TODAS las categorías
    definidas en CATEGORIES, en orden. Devuelve una lista de embeds (omite
    categorías sin datos en ese período).
    include_links=False se usa para el snapshot automático (ver build_leaderboard_embed).
    """
    embeds = []
    for name, col, *_ in CATEGORIES:
        rows = await fetch_leaderboard(pool, col, period_value, limit)
        if not rows:
            continue
        embeds.append(build_leaderboard_embed(rows, col, name, period_value, limit, now_uy, include_links))
    return embeds