"""db.py — Queries SQL del collector. Sin lógica de negocio ni HTTP."""
import json
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

log = logging.getLogger(__name__)

# Columnas de match_player_stats que mapean a métricas simples
METRIC_COLUMN = {
    "kills":    "kills",
    "deaths":   "deaths",
    "matches":  None,       # COUNT DISTINCT
    "combat":   "combat_score",
    "offense":  "offense_score",
    "defense":  "defense_score",
    "support":  "support_score",
    "kd_ratio": None,       # calculado
}

# Columnas JSONB para métricas parametrizadas
JSONB_COLUMN = {
    "kills_weapon": "weapons",
    "kills_player": "most_killed_ids",
    "kills_type":   "kills_by_type",
}


async def match_exists(conn: asyncpg.Connection, match_id: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM matches WHERE match_id = $1", match_id
    ))


async def insert_match(conn: asyncpg.Connection, match_id: str, map_name: str,
                        start_time, end_time, allied_score, axis_score) -> None:
    await conn.execute(
        """
        INSERT INTO matches (match_id, map_name, start_time, end_time, allied_score, axis_score)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT DO NOTHING
        """,
        match_id, map_name, start_time, end_time, allied_score, axis_score,
    )


async def insert_player_stats(conn: asyncpg.Connection, match_id: str,
                               steam_id: str, player: dict,
                               most_killed_ids: dict, death_by_ids: dict) -> None:
    time_sec = int(player.get("time_seconds") or 0)
    await conn.execute(
        """
        INSERT INTO match_player_stats
            (match_id, steam_id, player_name, kills, deaths, teamkills,
             combat_score, offense_score, defense_score, support_score, time_seconds,
             kills_by_type, deaths_by_type, weapons, death_by_weapons,
             most_killed, death_by, most_killed_ids, death_by_ids)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
        ON CONFLICT (match_id, steam_id) DO NOTHING
        """,
        match_id, steam_id, player.get("player", ""),
        int(player.get("kills") or 0),
        int(player.get("deaths") or 0),
        int(player.get("teamkills") or 0),
        int(player.get("combat") or 0),
        int(player.get("offense") or 0),
        int(player.get("defense") or 0),
        int(player.get("support") or 0),
        time_sec,
        json.dumps(player.get("kills_by_type") or {}),
        json.dumps(player.get("deaths_by_type") or {}),
        json.dumps(player.get("weapons") or {}),
        json.dumps(player.get("death_by_weapons") or {}),
        json.dumps(player.get("most_killed") or {}),
        json.dumps(player.get("death_by") or {}),
        json.dumps(most_killed_ids),
        json.dumps(death_by_ids),
    )


async def upsert_player(conn: asyncpg.Connection, steam_id: str,
                         player_name: str, match_start) -> None:
    """Actualiza el nombre del jugador solo si esta partida es más reciente."""
    await conn.execute(
        """
        INSERT INTO players (steam_id, player_name, last_match_start)
        VALUES ($1, $2, $3)
        ON CONFLICT (steam_id) DO UPDATE
            SET player_name = $2, last_match_start = $3
            WHERE players.last_match_start IS NULL
               OR $3 > players.last_match_start
        """,
        steam_id, player_name, match_start,
    )


async def insert_event(conn: asyncpg.Connection, guild_id: int,
                        event_type: str, message: str) -> None:
    await conn.execute(
        "INSERT INTO detected_events (guild_id, event_type, message) VALUES ($1, $2, $3)",
        guild_id, event_type, message,
    )


async def get_guilds_with_event_channel(conn: asyncpg.Connection) -> list:
    return await conn.fetch(
        "SELECT guild_id FROM guild_config WHERE eventos_channel_id IS NOT NULL"
    )


async def get_active_challenges(conn: asyncpg.Connection) -> list:
    return await conn.fetch(
        """
        SELECT * FROM challenges
        WHERE active = TRUE
          AND (end_date IS NULL OR end_date > NOW())
        """
    )


async def get_challenge_metrics(conn: asyncpg.Connection, challenge_id: int) -> list:
    return await conn.fetch(
        "SELECT id, metric, target, param FROM challenge_metrics WHERE challenge_id = $1",
        challenge_id,
    )


async def upsert_metric_progress(conn: asyncpg.Connection, metric_id: int,
                                  steam_id: str, player_name: str,
                                  progress: float, completed: bool) -> None:
    await conn.execute(
        """
        INSERT INTO challenge_metric_progress
            (challenge_metric_id, steam_id, player_name, progress, completed)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (challenge_metric_id, steam_id) DO UPDATE
            SET progress = $4, player_name = $3, completed = $5, updated_at = NOW()
        """,
        metric_id, steam_id, player_name, progress, completed,
    )


async def upsert_challenge_progress(conn: asyncpg.Connection, challenge_id: int,
                                     steam_id: str, player_name: str,
                                     all_completed: bool) -> None:
    await conn.execute(
        """
        INSERT INTO challenge_progress
            (challenge_id, steam_id, player_name, completed, completed_at)
        VALUES ($1, $2, $3, $4, CASE WHEN $4 THEN NOW() ELSE NULL END)
        ON CONFLICT (challenge_id, steam_id) DO UPDATE
            SET player_name = $3,
                completed = $4,
                completed_at = CASE
                    WHEN $4 AND challenge_progress.completed = FALSE
                    THEN NOW()
                    ELSE challenge_progress.completed_at
                END,
                updated_at = NOW()
        """,
        challenge_id, steam_id, player_name, all_completed,
    )


async def close_challenge(conn: asyncpg.Connection, challenge_id: int) -> None:
    await conn.execute(
        """
        UPDATE challenges
        SET active = FALSE, pending_close_notification = TRUE, closed_at = NOW()
        WHERE id = $1
        """,
        challenge_id,
    )


async def close_expired_custom_challenges(conn: asyncpg.Connection) -> None:
    expired = await conn.fetch(
        "SELECT id, name FROM challenges WHERE active = TRUE AND period = 'custom' AND end_date <= NOW()"
    )
    for ch in expired:
        await close_challenge(conn, ch["id"])
        log.info(f"  Desafío '{ch['name']}' (#{ch['id']}) cerrado: venció su fecha_fin")


async def expire_stale_close_notifications(conn: asyncpg.Connection) -> None:
    result = await conn.execute(
        """
        UPDATE challenges
        SET pending_close_notification = FALSE
        WHERE pending_close_notification = TRUE
          AND closed_at IS NOT NULL
          AND closed_at <= NOW() - INTERVAL '30 minutes'
        """
    )
    if result != "UPDATE 0":
        log.info(f"  Notificaciones de cierre descartadas por vencimiento: {result}")


async def resolve_match_scope(conn: asyncpg.Connection, challenge: dict) -> tuple:
    """
    Para desafíos current_match ya activados, busca si la partida cerró.
    Devuelve (match_ids, should_close).
    """
    if challenge["map_start"] is None:
        return None, False

    if challenge["match_id"] is not None:
        return [challenge["match_id"]], False

    map_start_dt = datetime.fromtimestamp(challenge["map_start"], tz=timezone.utc)
    closed = await conn.fetchrow(
        """
        SELECT match_id FROM matches
        WHERE start_time BETWEEN $1 AND $2
        ORDER BY start_time ASC LIMIT 1
        """,
        map_start_dt - timedelta(seconds=30),
        map_start_dt + timedelta(seconds=30),
    )

    if not closed:
        return None, False

    await conn.execute(
        "UPDATE challenges SET match_id = $1 WHERE id = $2",
        closed["match_id"], challenge["id"],
    )
    return [closed["match_id"]], True


async def fetch_metric_values(conn: asyncpg.Connection, metric: str,
                               match_ids: list = None,
                               start_date=None, end_date=None,
                               param: str = None) -> list:
    """
    Devuelve [{steam_id, player_name, value}] para una métrica.
    Filtra por match_ids o por rango de fechas [start_date, end_date].
    """
    if metric in JSONB_COLUMN:
        if not param:
            return []
        jsonb_col = JSONB_COLUMN[metric]
        if match_ids is not None:
            where_clause = "mps.match_id = ANY($1::varchar[])"
            params = [match_ids, param]
            param_n = 2
        else:
            where_clause = "m.start_time BETWEEN $1 AND $2"
            params = [start_date, end_date, param]
            param_n = 3
        query = f"""
            SELECT mps.steam_id,
                   COALESCE(MAX(p.player_name), MAX(mps.player_name)) AS player_name,
                   COALESCE(SUM((mps.{jsonb_col}->>${param_n})::int), 0) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            LEFT JOIN players p ON p.steam_id = mps.steam_id
            WHERE {where_clause}
              AND mps.{jsonb_col} ? ${param_n}
            GROUP BY mps.steam_id
        """
        return await conn.fetch(query, *params)

    col = METRIC_COLUMN.get(metric)

    if match_ids is not None:
        where_clause = "mps.match_id = ANY($1::varchar[])"
        params = [match_ids]
    else:
        where_clause = "m.start_time BETWEEN $1 AND $2"
        params = [start_date, end_date]

    if metric == "matches":
        query = f"""
            SELECT mps.steam_id,
                   COALESCE(MAX(p.player_name), MAX(mps.player_name)) AS player_name,
                   COUNT(DISTINCT mps.match_id) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            LEFT JOIN players p ON p.steam_id = mps.steam_id
            WHERE {where_clause}
            GROUP BY mps.steam_id
        """
    elif metric == "kd_ratio":
        query = f"""
            SELECT mps.steam_id,
                   COALESCE(MAX(p.player_name), MAX(mps.player_name)) AS player_name,
                   CASE WHEN SUM(mps.deaths) = 0 THEN SUM(mps.kills)::FLOAT
                        ELSE ROUND((SUM(mps.kills)::NUMERIC / SUM(mps.deaths)), 2)
                   END AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            LEFT JOIN players p ON p.steam_id = mps.steam_id
            WHERE {where_clause}
            GROUP BY mps.steam_id
        """
    else:
        query = f"""
            SELECT mps.steam_id,
                   COALESCE(MAX(p.player_name), MAX(mps.player_name)) AS player_name,
                   SUM(mps.{col}) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            LEFT JOIN players p ON p.steam_id = mps.steam_id
            WHERE {where_clause}
            GROUP BY mps.steam_id
        """

    return await conn.fetch(query, *params)


async def fetch_eligible_live_challenges(conn: asyncpg.Connection,
                                          map_start: int,
                                          map_start_dt) -> list:
    """Desafíos activos que aplican para la partida en vivo actual."""
    return await conn.fetch(
        """
        SELECT * FROM challenges
        WHERE active = TRUE
          AND (
                (period = 'current_match' AND match_id IS NULL AND map_start = $1)
             OR (period = 'custom' AND start_date <= $2)
          )
        """,
        map_start, map_start_dt,
    )


async def fetch_challenges_needing_kill_logs(conn: asyncpg.Connection,
                                              challenge_ids: list) -> bool:
    """True si alguno de los desafíos tiene métricas que requieren logs de kills en vivo."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM challenge_metrics cm
            JOIN challenges c ON c.id = cm.challenge_id
            WHERE c.id = ANY($1::int[])
              AND cm.metric IN ('kills_weapon', 'kills_player', 'kills_type')
        )
        """,
        challenge_ids,
    )


async def fetch_backfill_candidates(conn: asyncpg.Connection) -> list:
    """Partidas con columnas JSONB vacías (guardadas antes del schema actual)."""
    return await conn.fetch(
        """
        SELECT DISTINCT match_id
        FROM match_player_stats
        WHERE kills_by_type = '{}'::jsonb OR kills_by_type IS NULL
        ORDER BY match_id::int ASC
        """
    )


async def fetch_closed_deaths(conn: asyncpg.Connection,
                               match_ids: list = None,
                               start_date=None, end_date=None) -> list:
    """Deaths cerrados para el cálculo de kd_ratio combinado (cerrado + en vivo)."""
    if match_ids is not None:
        where = "mps.match_id = ANY($1::varchar[])"
        params = [match_ids]
    else:
        where = "m.start_time BETWEEN $1 AND $2"
        params = [start_date, end_date]
    return await conn.fetch(
        f"""
        SELECT mps.steam_id,
               COALESCE(MAX(p.player_name), MAX(mps.player_name)) AS player_name,
               SUM(mps.deaths) AS value
        FROM match_player_stats mps
        JOIN matches m USING (match_id)
        LEFT JOIN players p ON p.steam_id = mps.steam_id
        WHERE {where}
        GROUP BY mps.steam_id
        """,
        *params,
    )