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
# se calculan los ranks individuales en /stats show (incluye K/D).
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

# Categorías para /hll top y los snapshots diarios/semanales/mensuales —
# sin K/D, porque con pocas partidas (o pocas deaths) el ratio se infla
# de forma poco representativa y no sirve como ranking confiable del
# servidor. El rank individual de K/D en /stats show sí se mantiene
# (ahí no compite por el "mejor del server", solo informa tu posición).
SNAPSHOT_CATEGORIES = [c for c in CATEGORIES if c[1] != "kd_ratio"]

# Lista de armas/categorías de kill de HLL, para autocompletado del
# parámetro 'arma' (en /hlladmin desafio crear y /hll weapon). Nombres
# exactos como aparecen en los logs de CRCON (sensible a mayúsculas).
HLL_WEAPONS = [
    # Submachine Guns
    "M1A1 THOMPSON", "M3 GREASE GUN", "MP40", "PPSH 41", "PPSH 41 W/DRUM",
    "Sten Gun Mk.II", "Sten Gun Mk.V", "Lanchester", "M1928A1 THOMPSON",
    # Semi-Auto Rifles
    "M1 GARAND", "M1 CARBINE", "GEWEHR 43", "SVT40",
    # Bolt-Action Rifles
    "KARABINER 98K", "MOSIN NAGANT 1891", "MOSIN NAGANT 91/30", "MOSIN NAGANT M38",
    "SMLE No.1 Mk III", "Rifle No.4 Mk I", "Rifle No.5 Mk I",
    # Assault Rifles
    "M1918A2 BAR", "STG44", "FG42", "Bren Gun",
    # Shotguns
    "M97 TRENCH GUN",
    # Machine Guns
    "BROWNING M1919", "MG34", "MG42", "DP-27", "Lewis Gun",
    # Sniper Rifles
    "M1903 SPRINGFIELD", "KARABINER 98K x8", "FG42 x4",
    "SCOPED MOSIN NAGANT 91/30", "SCOPED SVT40",
    "Lee-Enfield Pattern 1914 Sniper", "Rifle No.4 Mk I Sniper",
    # Pistols
    "COLT M1911", "WALTHER P38", "LUGER P08", "NAGANT M1895", "TOKAREV TT33", "Webley MK VI",
    # Flamethrowers
    "M2 FLAMETHROWER", "FLAMMENWERFER 41", "FLAMETHROWER",
    # Melee
    "M3 KNIFE", "FELDSPATEN", "MPL-50 SPADE", "Fairbairn–Sykes",
    # Grenades
    "MK2 GRENADE", "M24 STIELHANDGRANATE", "M43 STIELHANDGRANATE",
    "RG-42 GRENADE", "MOLOTOV", "Mills Bomb", "No.82 Grenade",
    # Satchel Charges
    "SATCHEL", "SATCHEL CHARGE",
    # Anti-Personnel Mines
    "M2 AP MINE", "S-MINE", "POMZ AP MINE", "A.P. Shrapnel Mine Mk II",
    # Anti-Tank Mines
    "M1A1 AT MINE", "TELLERMINE 43", "TM-35 AT MINE", "A.T. Mine G.S. Mk V",
    # Anti-Tank Rifles / Rocket Launchers
    "BAZOOKA", "PANZERSCHRECK", "PTRS-41", "PIAT", "Boys Anti-tank Rifle",
    # Flare Guns
    "FLARE GUN", "No.2 Mk 5 Flare Pistol",
    # Artillery / AT Guns
    "155MM HOWITZER [M114]", "150MM HOWITZER [sFH 18]", "122MM HOWITZER [M1938 (M-30)]",
    "QF 25-POUNDER [QF 25-Pounder]", "57MM CANNON [M1 57mm]", "75MM CANNON [PAK 40]",
    "57MM CANNON [ZiS-2]", "QF 6-POUNDER [QF 6-Pounder]",
    # Commander Abilities
    "BOMBING RUN", "STRAFING RUN", "PRECISION STRIKE",
]


async def get_top_killers_by_weapon(pool, weapon: str, limit: int = 10) -> list:
    """
    Devuelve el Top N de jugadores con mas kills usando un arma exacta,
    para /hll weapon. Lista de dicts {steam_id, player_name, kills, matches}.
    Usa la columna JSONB 'weapons' de match_player_stats.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT steam_id, MAX(player_name) AS player_name,
                   SUM((weapons->>$1)::int) AS kills,
                   COUNT(*) AS matches
            FROM match_player_stats
            WHERE weapons ? $1
            GROUP BY steam_id
            ORDER BY kills DESC
            LIMIT $2
            """,
            weapon, limit
        )
    return [
        {"steam_id": r["steam_id"], "player_name": r["player_name"],
         "kills": r["kills"], "matches": r["matches"]}
        for r in rows
    ]


async def get_all_weapons_with_rank(pool, steam_id: str) -> list:
    """
    Devuelve TODAS las armas con las que el jugador tiene al menos un
    kill, con su rank entre todos los jugadores que usaron esa arma.
    Usa la columna JSONB 'weapons' de match_player_stats.
    Lista de dicts {weapon, kills, rank, total_players}, ordenada por
    kills descendente.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH kills_per_player_weapon AS (
                SELECT steam_id, key AS weapon, SUM(value::int) AS kills
                FROM match_player_stats,
                     jsonb_each_text(weapons) AS t(key, value)
                GROUP BY steam_id, key
            ),
            ranked AS (
                SELECT steam_id, weapon, kills,
                       RANK() OVER (PARTITION BY weapon ORDER BY kills DESC) AS rank,
                       COUNT(*) OVER (PARTITION BY weapon) AS total_players
                FROM kills_per_player_weapon
            )
            SELECT weapon, kills, rank, total_players
            FROM ranked
            WHERE steam_id = $1
            ORDER BY kills DESC
            """,
            steam_id
        )
    return [
        {"weapon": r["weapon"], "kills": r["kills"],
         "rank": r["rank"], "total_players": r["total_players"]}
        for r in rows
    ]


async def get_player_ranks(pool, steam_id: str) -> dict:
    """
    Calcula la posición (rank) del jugador en cada categoría de
    player_totals, excluyendo de cada ranking a los jugadores con 0 en
    esa columna específica (no un mínimo de partidas — un jugador con
    0 kills no entra al ranking de Kills, pero si tiene Support > 0 sí
    entra al de Support). Incluye matches_played y total_time_seconds
    (horas jugadas), además de las columnas de CATEGORIES.

    Devuelve {column: (rank, total_jugadores_en_ese_ranking) | None}.
    None si el jugador no tiene ese valor > 0 (no rankeado en esa categoría).
    """
    rankable_columns = [col for _, col, *_ in CATEGORIES] + ["total_time_seconds"]

    ranks = {}
    async with pool.acquire() as conn:
        for col in rankable_columns:
            row = await conn.fetchrow(
                f"""
                SELECT rank, total FROM (
                    SELECT steam_id,
                           RANK() OVER (ORDER BY {col} DESC) AS rank,
                           COUNT(*) OVER () AS total
                    FROM player_totals
                    WHERE {col} > 0
                ) ranked
                WHERE steam_id = $1
                """,
                steam_id
            )
            ranks[col] = (row["rank"], row["total"]) if row else None
    return ranks

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
      - day:   00:00 del día de 'now'
      - week:  lunes 00:00 de la semana que contiene a 'now'
      - month: día 1, 00:00 del mes que contiene a 'now'
    Si 'now' no se pasa, usa el momento actual.
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


def period_end(period_value: str, now: datetime = None) -> datetime:
    """
    Calcula el límite superior EXCLUSIVO del período (el inicio del
    período siguiente), en hora UY. Usado junto a period_start() para
    delimitar un rango cerrado de calendario (ej: un día completo de
    00:00:00 a 23:59:59.999..., expresado como [inicio, inicio_siguiente)).
    """
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


async def fetch_leaderboard(pool, col: str, period_value: str, limit: int,
                             reference_date: datetime = None):
    """
    Devuelve las filas del ranking para una columna/categoría y período dados.
    period_value: 'all' | 'day' | 'week' | 'month'
    reference_date: si se pasa, el día/semana/mes se calcula en base a esa
    fecha (hora UY) en vez de "ahora". Por ejemplo, period_value='week' con
    reference_date=20/06/2026 devuelve la semana (lun-dom) que contiene esa
    fecha, sin importar qué día es hoy.
    """
    if period_value == "all":
        async with pool.acquire() as conn:
            return await conn.fetch(
                f"""
                SELECT steam_id, last_name, {col}, matches_played,
                       total_kills, total_deaths, kd_ratio, total_time_seconds
                FROM player_totals
                ORDER BY {col} DESC NULLS LAST
                LIMIT $1
                """,
                limit
            )

    # Períodos de CALENDARIO (no ventana móvil), calculados en hora UY (UTC-3).
    # Rango cerrado-abierto [desde, hasta) para no depender de redondeos en
    # el límite superior (ej: una partida que arranca 23:59:50 del último
    # día del rango sigue entrando, porque 'hasta' es el inicio del período
    # SIGUIENTE, no las 23:59:59 del mismo día).
    desde = period_start(period_value, reference_date)
    hasta = period_end(period_value, reference_date)

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
                SUM(mps.support_score)                              AS total_support,
                SUM(mps.time_seconds)                                AS total_time_seconds
            FROM match_player_stats mps
            JOIN matches m ON m.match_id = mps.match_id
            WHERE m.start_time >= $2 AND m.start_time < $3
            GROUP BY mps.steam_id
            ORDER BY {col} DESC NULLS LAST
            LIMIT $1
            """,
            limit, desde, hasta
        )


def build_leaderboard_embed(rows, col: str, categoria_name: str, period_value: str,
                             limit: int, now_uy: datetime = None,
                             include_links: bool = True,
                             reference_date: datetime = None) -> discord.Embed:
    """
    Construye el embed de un ranking ya consultado (rows de fetch_leaderboard).
    include_links=False usa texto plano sin link a Steam (más corto): se usa
    en el snapshot automático, donde 7 embeds van en un solo mensaje y Discord
    limita el total combinado a 6000 caracteres.
    reference_date: si se pasó al consultar (fetch_leaderboard), debe pasarse
    también aquí para que el footer muestre el rango correcto (día/semana/mes
    de esa fecha, no de "ahora").
    """
    _, color, icon, value_label = CATEGORY_BY_COLUMN.get(col, (categoria_name, 0xF1C40F, "🏆", categoria_name))
    period_label = PERIOD_LABELS.get(period_value, "Histórico")

    # Qué dato(s) extra mostrar al lado del valor principal, por columna.
    # Cada entrada: (etiqueta_header, función que devuelve el texto de la línea)
    def fmt_partidas(r):
        return f"{r['matches_played']} partidas"

    def fmt_kd(r):
        kd_val = r["kd_ratio"]
        return f"KD {kd_val:.2f}" if kd_val is not None else "KD —"

    def fmt_deaths(r):
        return f"{r['total_deaths']} deaths"

    def fmt_horas(r):
        horas = round((r["total_time_seconds"] or 0) / 3600, 1)
        return f"{horas}h"

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

        lines.append(
            f"{pos}. {name_display} — **{value_str}**{extra_str}"
        )

    embed = discord.Embed(
        title=f"{icon} Top {limit} — {categoria_name} ({period_label})",
        description="\n".join(lines),
        color=color
    )

    if period_value == "all":
        footer_txt = "📊 Stats históricos acumulados • actualizado cada 30 min"
    else:
        desde = period_start(period_value, reference_date)
        hasta_exclusive = period_end(period_value, reference_date)
        now_uy = now_uy or datetime.now(TZ_UY)

        if reference_date is not None:
            # Período de una fecha específica del pasado (o futuro): mostramos
            # el rango completo de calendario, sin decir "calculado en vivo".
            hasta_mostrar = min(hasta_exclusive, now_uy)  # no mostrar más allá de "ahora" si coincide con hoy
            rango = f"Desde: {desde.strftime('%d/%m %H:%M:%S')} — Hasta: {hasta_mostrar.strftime('%d/%m %H:%M:%S')}"
            footer_txt = f"📊 Stats {FOOTER_PERIOD_TEXT[period_value]} ({desde.strftime('%d/%m/%Y')})\n{rango}"
        else:
            rango = f"Desde: {desde.strftime('%d/%m %H:%M:%S')} — Hasta: {now_uy.strftime('%d/%m %H:%M:%S')}"
            footer_txt = f"📊 Stats {FOOTER_PERIOD_TEXT[period_value]} • calculado en vivo\n{rango}"

    embed.set_footer(text=footer_txt)
    return embed


async def build_all_category_embeds(pool, period_value: str, limit: int, now_uy: datetime = None,
                                      include_links: bool = True, reference_date: datetime = None):
    """
    Corre fetch_leaderboard + build_leaderboard_embed para TODAS las categorías
    definidas en SNAPSHOT_CATEGORIES (sin K/D), en orden. Devuelve una lista
    de embeds (omite categorías sin datos en ese período).
    include_links=False se usa para el snapshot automático (ver build_leaderboard_embed).
    reference_date: ver fetch_leaderboard / build_leaderboard_embed.
    """
    embeds = []
    for name, col, *_ in SNAPSHOT_CATEGORIES:
        rows = await fetch_leaderboard(pool, col, period_value, limit, reference_date)
        if not rows:
            continue
        embeds.append(build_leaderboard_embed(
            rows, col, name, period_value, limit, now_uy, include_links, reference_date
        ))
    return embeds