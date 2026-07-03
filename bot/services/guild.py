"""services/guild.py — Lógica de configuración del servidor y vinculaciones."""
import logging

import discord

from db import guild as db_guild
from db import players as db_players
from timeutils import format_local

log = logging.getLogger(__name__)


async def get_vinculados_embed(pool) -> discord.Embed:
    """Construye el embed de cuentas vinculadas Discord<->Steam."""
    async with pool.acquire() as conn:
        rows = await db_players.get_linked_players(conn)

    if rows:
        lines = []
        for r in rows:
            steam_display = f"{r['steam_name']} " if r['steam_name'] else ""
            lines.append(
                f"`{r['discord_name'] or '?'}` — {steam_display}`{r['steam_id']}` "
                f"_(vinculado {format_local(r['linked_at'], '%d/%m/%Y %H:%M')})_"
            )

        description = "\n".join(lines)
        if len(description) > 4000:
            shown, total_len = [], 0
            for line in lines:
                if total_len + len(line) + 1 > 3950:
                    break
                shown.append(line)
                total_len += len(line) + 1
            faltantes = len(lines) - len(shown)
            description = "\n".join(shown) + f"\n\n_... y {faltantes} más_"
    else:
        description = "_Todavía no hay nadie vinculado._"
        lines = []

    embed = discord.Embed(
        title="🔗 Cuentas vinculadas (Discord ↔ Steam)",
        description=description,
        color=0x5865F2,
    )
    embed.set_footer(
        text=f"{len(rows)} cuenta(s) vinculada(s) • Ordenado por más reciente"
    )
    return embed


async def update_vinculados_message(bot, pool, guild_id: int) -> None:
    """
    Edita (o crea) el mensaje fijo con la lista de cuentas vinculadas
    en el canal configurado. Se llama tras cada /hll registro.
    """
    async with pool.acquire() as conn:
        config = await db_guild.get_vinculados_config(conn, guild_id)

    if not config or not config["vinculados_channel_id"]:
        return

    embed = await get_vinculados_embed(pool)

    channel = bot.get_channel(config["vinculados_channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(config["vinculados_channel_id"])
        except discord.HTTPException:
            return

    message_id = config["vinculados_message_id"]
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            return
        except discord.NotFound:
            pass
        except discord.HTTPException:
            return

    new_message = await channel.send(embed=embed)
    async with pool.acquire() as conn:
        await db_guild.set_vinculados_message_id(conn, guild_id, new_message.id)


async def register_player(pool, discord_id: int, discord_name: str,
                           steam_id: str) -> tuple[bool, str]:
    """
    Vincula un discord_id con un steam_id.
    Devuelve (success: bool, player_name: str | error_msg: str).
    """
    async with pool.acquire() as conn:
        player = await db_players.get_player_by_steam_id(conn, steam_id)
        if not player:
            return False, (
                "❌ Ese ID no aparece en nuestros registros — todavía no detectamos "
                "ninguna partida tuya en el servidor. Jugá al menos una partida y "
                "probá de nuevo en unos minutos."
            )

        existing_discord = await db_guild.get_linked_discord_for_steam(conn, steam_id)
        if existing_discord and existing_discord != discord_id:
            return False, "❌ Ese Steam ID ya está vinculado a otra cuenta."

        await db_guild.link_player(conn, discord_id, steam_id, discord_name)

    return True, player["player_name"] or "?"