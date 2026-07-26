"""db/challenges.py — Queries SQL de desafíos y su progreso."""
import asyncpg


async def get_active_challenges(conn: asyncpg.Connection, guild_id: int) -> list:
    return await conn.fetch(
        """
        SELECT * FROM challenges
        WHERE guild_id = $1 AND active = TRUE
          AND (end_date IS NULL OR end_date > NOW())
        ORDER BY id DESC
        """,
        guild_id,
    )


async def get_challenge(conn: asyncpg.Connection,
                         challenge_id: int, guild_id: int):
    return await conn.fetchrow(
        "SELECT * FROM challenges WHERE id = $1 AND guild_id = $2",
        challenge_id, guild_id,
    )


async def get_challenge_metrics(conn: asyncpg.Connection, challenge_id: int) -> list:
    return await conn.fetch(
        "SELECT id, metric, target, param FROM challenge_metrics WHERE challenge_id = $1",
        challenge_id,
    )


async def get_challenge_overall_progress(conn: asyncpg.Connection,
                                          challenge_id: int) -> list:
    return await conn.fetch(
        """
        SELECT steam_id, player_name, completed
        FROM challenge_progress
        WHERE challenge_id = $1
        """,
        challenge_id,
    )


async def get_metric_progress(conn: asyncpg.Connection,
                               challenge_metric_id: int) -> list:
    return await conn.fetch(
        """
        SELECT steam_id, player_name, progress
        FROM challenge_metric_progress
        WHERE challenge_metric_id = $1
        """,
        challenge_metric_id,
    )


async def get_guild_challenge_channel(conn: asyncpg.Connection, guild_id: int) -> int | None:
    row = await conn.fetchrow(
        "SELECT challenge_channel_id FROM guild_config WHERE guild_id = $1", guild_id
    )
    return (row or {}).get("challenge_channel_id")


async def resolve_player_names(conn: asyncpg.Connection, steam_ids: list) -> dict:
    """Devuelve {steam_id: player_name} para una lista de IDs."""
    if not steam_ids:
        return {}
    rows = await conn.fetch(
        "SELECT steam_id, player_name FROM players WHERE steam_id = ANY($1::varchar[])",
        steam_ids,
    )
    return {r["steam_id"]: r["player_name"] for r in rows}


async def deactivate_challenge(conn: asyncpg.Connection,
                                challenge_id: int, guild_id: int) -> bool:
    result = await conn.execute(
        "UPDATE challenges SET active = FALSE WHERE id = $1 AND guild_id = $2",
        challenge_id, guild_id,
    )
    return result != "UPDATE 0"


async def create_challenge(conn: asyncpg.Connection,
                            guild_id: int, name: str, period: str,
                            start_date, end_date, match_id,
                            created_by: int,
                            map_name: str = None,
                            map_start=None,
                            premio_vip_dias: int = 0) -> int:
    if map_name is not None:
        row = await conn.fetchrow(
            """
            INSERT INTO challenges
                (guild_id, name, description, period, start_date, end_date,
                 match_id, created_by, map_name, map_start, premio_vip_dias)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            guild_id, name, None, period,
            start_date, end_date, match_id, created_by, map_name, map_start, premio_vip_dias,
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO challenges
                (guild_id, name, description, period, start_date, end_date, match_id, created_by, premio_vip_dias)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
            """,
            guild_id, name, None, period,
            start_date, end_date, match_id, created_by, premio_vip_dias,
        )
    return row["id"]


async def add_challenge_metric(conn: asyncpg.Connection,
                                challenge_id: int, metric: str,
                                target: float, param: str = None) -> None:
    await conn.execute(
        "INSERT INTO challenge_metrics (challenge_id, metric, target, param) VALUES ($1, $2, $3, $4)",
        challenge_id, metric, target, param,
    )