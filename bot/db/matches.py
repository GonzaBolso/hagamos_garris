"""db/matches.py — Queries SQL de partidas y stats de jugadores por partida."""
import asyncpg


async def get_recent_matches_for_player(conn: asyncpg.Connection,
                                         steam_id: str, limit: int = 5) -> list:
    return await conn.fetch(
        """
        SELECT
            m.map_name, m.start_time, m.allied_score, m.axis_score,
            mps.kills, mps.deaths, mps.combat_score,
            mps.offense_score, mps.defense_score, mps.support_score, mps.time_seconds
        FROM match_player_stats mps
        JOIN matches m USING (match_id)
        WHERE mps.steam_id = $1
          AND (mps.kills != 0 OR mps.deaths != 0 OR mps.combat_score != 0
               OR mps.offense_score != 0 OR mps.defense_score != 0
               OR mps.support_score != 0)
        ORDER BY m.start_time DESC NULLS LAST
        LIMIT $2
        """,
        steam_id, limit,
    )


async def get_top_weapons_for_player(conn: asyncpg.Connection,
                                      steam_id: str, limit: int = 5) -> list:
    rows = await conn.fetch(
        """
        SELECT key AS weapon, SUM(value::int) AS kills
        FROM match_player_stats,
             jsonb_each_text(weapons) AS t(key, value)
        WHERE steam_id = $1
        GROUP BY key
        ORDER BY kills DESC
        LIMIT $2
        """,
        steam_id, limit,
    )
    return [{"weapon": r["weapon"], "kills": r["kills"]} for r in rows]


async def get_all_weapons_with_rank(conn: asyncpg.Connection, steam_id: str) -> list:
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
        steam_id,
    )
    return [
        {"weapon": r["weapon"], "kills": r["kills"],
         "rank": r["rank"], "total_players": r["total_players"]}
        for r in rows
    ]


async def get_top_killers_by_weapon(conn: asyncpg.Connection,
                                     weapon: str, limit: int = 10) -> list:
    rows = await conn.fetch(
        """
        SELECT mps.steam_id,
               COALESCE(MAX(p.player_name), MAX(mps.player_name)) AS player_name,
               SUM((mps.weapons->>$1)::int) AS kills,
               COUNT(*) AS matches
        FROM match_player_stats mps
        LEFT JOIN players p ON p.steam_id = mps.steam_id
        WHERE mps.weapons ? $1
        GROUP BY mps.steam_id
        ORDER BY kills DESC
        LIMIT $2
        """,
        weapon, limit,
    )
    return [
        {"steam_id": r["steam_id"], "player_name": r["player_name"],
         "kills": r["kills"], "matches": r["matches"]}
        for r in rows
    ]


async def get_all_weapons_totals(conn: asyncpg.Connection) -> list:
    """Lista de todas las armas con kills totales, ordenadas por kills DESC."""
    return await conn.fetch(
        """
        SELECT key AS weapon, SUM(value::int) AS total_kills
        FROM match_player_stats,
             jsonb_each_text(weapons) AS t(key, value)
        GROUP BY key
        ORDER BY total_kills DESC
        """
    )


async def fetch_leaderboard(conn: asyncpg.Connection, col: str,
                              period_value: str, limit: int,
                              desde=None, hasta=None) -> list:
    """
    Devuelve filas del ranking para una columna y período.
    Si period_value == 'all' usa player_totals, si no filtra por rango de fechas.
    col debe ser un valor de CATEGORIES — nunca viene del usuario directamente.
    """
    if period_value == "all":
        return await conn.fetch(
            f"""
            SELECT steam_id, last_name, {col}, matches_played,
                   total_kills, total_deaths, kd_ratio, total_time_seconds
            FROM player_totals
            ORDER BY {col} DESC NULLS LAST
            LIMIT $1
            """,
            limit,
        )

    return await conn.fetch(
        f"""
        SELECT
            mps.steam_id,
            COALESCE(MAX(p.player_name), MAX(mps.player_name))      AS last_name,
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
        LEFT JOIN players p ON p.steam_id = mps.steam_id
        WHERE m.start_time >= $2 AND m.start_time < $3
        GROUP BY mps.steam_id
        ORDER BY {col} DESC NULLS LAST
        LIMIT $1
        """,
        limit, desde, hasta,
    )


async def get_player_rank(conn: asyncpg.Connection, steam_id: str, col: str):
    """
    Devuelve (rank, total) del jugador en una columna de player_totals,
    o None si el jugador no tiene valor > 0 en esa columna.
    col es siempre una constante interna (CATEGORIES), nunca input del usuario.
    """
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
        steam_id,
    )
    return (row["rank"], row["total"]) if row else None