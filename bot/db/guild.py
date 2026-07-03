"""db/guild.py — Queries SQL de configuración del servidor (guild_config)."""
import asyncpg


async def get_guild_config(conn: asyncpg.Connection, guild_id: int):
    return await conn.fetchrow(
        "SELECT * FROM guild_config WHERE guild_id = $1", guild_id
    )


async def get_stats_channel(conn: asyncpg.Connection, guild_id: int) -> int | None:
    row = await conn.fetchrow(
        "SELECT stats_channel_id FROM guild_config WHERE guild_id = $1", guild_id
    )
    return (row or {}).get("stats_channel_id")


async def get_snapshot_channel(conn: asyncpg.Connection, guild_id: int) -> int | None:
    row = await conn.fetchrow(
        "SELECT snapshot_channel_id FROM guild_config WHERE guild_id = $1", guild_id
    )
    return (row or {}).get("snapshot_channel_id")


async def get_vinculados_config(conn: asyncpg.Connection, guild_id: int):
    return await conn.fetchrow(
        "SELECT vinculados_channel_id, vinculados_message_id FROM guild_config WHERE guild_id = $1",
        guild_id,
    )


async def set_vinculados_message_id(conn: asyncpg.Connection,
                                     guild_id: int, message_id: int) -> None:
    await conn.execute(
        "UPDATE guild_config SET vinculados_message_id = $1 WHERE guild_id = $2",
        message_id, guild_id,
    )


async def upsert_channels(conn: asyncpg.Connection, guild_id: int,
                           stats_channel_id: int,
                           snapshot_channel_id: int | None,
                           challenge_channel_id: int | None,
                           vinculados_channel_id: int | None,
                           eventos_channel_id: int | None) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config
            (guild_id, stats_channel_id, snapshot_channel_id,
             challenge_channel_id, vinculados_channel_id, eventos_channel_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (guild_id) DO UPDATE
            SET stats_channel_id      = $2,
                snapshot_channel_id   = COALESCE($3, guild_config.snapshot_channel_id),
                challenge_channel_id  = COALESCE($4, guild_config.challenge_channel_id),
                vinculados_channel_id = COALESCE($5, guild_config.vinculados_channel_id),
                eventos_channel_id    = COALESCE($6, guild_config.eventos_channel_id),
                updated_at            = NOW()
        """,
        guild_id, stats_channel_id, snapshot_channel_id,
        challenge_channel_id, vinculados_channel_id, eventos_channel_id,
    )


async def upsert_roles(conn: asyncpg.Connection, guild_id: int,
                        admin_role_id: int, player_role_id: int) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config (guild_id, admin_role_id, mod_role_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (guild_id) DO UPDATE
            SET admin_role_id = $2, mod_role_id = $3, updated_at = NOW()
        """,
        guild_id, admin_role_id, player_role_id,
    )


async def get_linked_steam_id_for_discord(conn: asyncpg.Connection,
                                           discord_id: int) -> str | None:
    row = await conn.fetchrow(
        "SELECT steam_id FROM linked_players WHERE discord_id = $1", discord_id
    )
    return row["steam_id"] if row else None


async def get_linked_discord_for_steam(conn: asyncpg.Connection,
                                        steam_id: str) -> int | None:
    row = await conn.fetchrow(
        "SELECT discord_id FROM linked_players WHERE steam_id = $1", steam_id
    )
    return row["discord_id"] if row else None


async def link_player(conn: asyncpg.Connection, discord_id: int,
                       steam_id: str, discord_name: str) -> None:
    await conn.execute(
        """
        INSERT INTO linked_players (discord_id, steam_id, discord_name, linked_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (discord_id) DO UPDATE
            SET steam_id = $2, discord_name = $3, linked_at = NOW()
        """,
        discord_id, steam_id, discord_name,
    )