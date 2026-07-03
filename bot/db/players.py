"""db/players.py — Queries SQL de jugadores, vinculaciones y totales."""
import asyncpg


async def get_linked_steam_id(conn: asyncpg.Connection, discord_id: int) -> str | None:
    row = await conn.fetchrow(
        "SELECT steam_id FROM linked_players WHERE discord_id = $1", discord_id
    )
    return row["steam_id"] if row else None


async def get_player_totals(conn: asyncpg.Connection, steam_id: str):
    return await conn.fetchrow(
        "SELECT * FROM player_totals WHERE steam_id = $1", steam_id
    )


async def get_linked_players(conn: asyncpg.Connection) -> list:
    return await conn.fetch(
        """
        SELECT lp.discord_id, lp.discord_name, lp.steam_id, lp.linked_at,
               p.player_name AS steam_name
        FROM linked_players lp
        LEFT JOIN players p ON p.steam_id = lp.steam_id
        ORDER BY lp.linked_at DESC
        """
    )


async def link_player(conn: asyncpg.Connection, discord_id: int,
                       discord_name: str, steam_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO linked_players (discord_id, discord_name, steam_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (discord_id) DO UPDATE
            SET steam_id = $3, discord_name = $2
        """,
        discord_id, discord_name, steam_id,
    )


async def unlink_player(conn: asyncpg.Connection, discord_id: int) -> bool:
    result = await conn.execute(
        "DELETE FROM linked_players WHERE discord_id = $1", discord_id
    )
    return result != "DELETE 0"


async def search_players_by_name(conn: asyncpg.Connection,
                                   name: str, limit: int = 10) -> list:
    return await conn.fetch(
        """
        SELECT steam_id, player_name FROM players
        WHERE player_name ILIKE $1
        ORDER BY last_match_start DESC NULLS LAST
        LIMIT $2
        """,
        f"%{name}%", limit,
    )


async def get_player_by_steam_id(conn: asyncpg.Connection, steam_id: str):
    return await conn.fetchrow(
        "SELECT steam_id, player_name FROM players WHERE steam_id = $1", steam_id
    )