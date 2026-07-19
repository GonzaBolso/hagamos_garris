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
                           stats_channel_id: int | None = None,
                           snapshot_channel_id: int | None = None,
                           challenge_channel_id: int | None = None,
                           vinculados_channel_id: int | None = None,
                           eventos_channel_id: int | None = None,
                           server_status_channel_id: int | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO guild_config
            (guild_id, stats_channel_id, snapshot_channel_id,
             challenge_channel_id, vinculados_channel_id, eventos_channel_id,
             server_status_channel_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (guild_id) DO UPDATE
            SET stats_channel_id         = COALESCE($2, guild_config.stats_channel_id),
                snapshot_channel_id      = COALESCE($3, guild_config.snapshot_channel_id),
                challenge_channel_id     = COALESCE($4, guild_config.challenge_channel_id),
                vinculados_channel_id    = COALESCE($5, guild_config.vinculados_channel_id),
                eventos_channel_id       = COALESCE($6, guild_config.eventos_channel_id),
                server_status_channel_id = COALESCE($7, guild_config.server_status_channel_id),
                updated_at               = NOW()
        """,
        guild_id, stats_channel_id, snapshot_channel_id,
        challenge_channel_id, vinculados_channel_id, eventos_channel_id,
        server_status_channel_id,
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


async def get_server_status_config(conn: asyncpg.Connection, guild_id: int):
    return await conn.fetchrow(
        "SELECT server_status_channel_id, server_status_message_id FROM guild_config WHERE guild_id = $1",
        guild_id,
    )


async def set_server_status_message_id(conn: asyncpg.Connection,
                                        guild_id: int, message_id: int) -> None:
    await conn.execute(
        "UPDATE guild_config SET server_status_message_id = $1 WHERE guild_id = $2",
        message_id, guild_id,
    )


async def get_all_server_status_configs(conn: asyncpg.Connection) -> list:
    """Devuelve todos los guilds que tienen canal de status configurado."""
    return await conn.fetch(
        "SELECT guild_id, server_status_channel_id, server_status_message_id FROM guild_config WHERE server_status_channel_id IS NOT NULL"
    )


async def get_snapshot_last_fired(conn) -> "datetime.date | None":
    """Devuelve la última fecha en que se disparó el snapshot (persiste reinicios)."""
    import datetime
    row = await conn.fetchrow(
        "SELECT MAX(snapshot_last_fired) AS d FROM guild_config WHERE snapshot_last_fired IS NOT NULL"
    )
    return row["d"] if row else None


async def set_snapshot_last_fired(conn, date: "datetime.date") -> None:
    await conn.execute(
        "UPDATE guild_config SET snapshot_last_fired = $1 WHERE snapshot_last_fired IS NULL OR snapshot_last_fired < $1",
        date,
    )

async def get_seed_config(conn, guild_id: int):
    return await conn.fetchrow(
        """SELECT seed_role_id, seed_channel_id, seed_threshold, seed_last_notified
           FROM guild_config WHERE guild_id = $1""",
        guild_id,
    )


async def set_seed_last_notified(conn, guild_id: int) -> None:
    """Marca hoy como la fecha en que se mandó la notificación de seed."""
    import datetime
    await conn.execute(
        "UPDATE guild_config SET seed_last_notified = $1 WHERE guild_id = $2",
        datetime.date.today(), guild_id,
    )


async def set_seed_config(conn, guild_id: int,
                           role_id: int = None, channel_id: int = None,
                           threshold: int = None) -> None:
    await conn.execute(
        """INSERT INTO guild_config (guild_id, seed_role_id, seed_channel_id, seed_threshold)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (guild_id) DO UPDATE SET
               seed_role_id    = COALESCE($2, guild_config.seed_role_id),
               seed_channel_id = COALESCE($3, guild_config.seed_channel_id),
               seed_threshold  = COALESCE($4, guild_config.seed_threshold)
        """,
        guild_id, role_id, channel_id, threshold,
    )