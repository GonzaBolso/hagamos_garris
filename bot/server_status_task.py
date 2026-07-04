"""
server_status_task.py — Actualiza el panel de estado del servidor cada 60s.
Edita un mensaje fijo en el canal configurado con /hlladmin setchannel canal_status.
"""
import logging

import discord
from discord.ext import tasks

from api import crcon, CRCONError
from db import guild as db_guild
from services.server import build_server_status_embed, build_team_view_embeds

log = logging.getLogger(__name__)

UPDATE_INTERVAL_SECONDS = 60


def setup_server_status_task(bot, pool):
    @tasks.loop(seconds=UPDATE_INTERVAL_SECONDS)
    async def server_status_loop():
        try:
            async with pool.acquire() as conn:
                configs = await db_guild.get_all_server_status_configs(conn)

            if not configs:
                return

            try:
                info      = await crcon.get_public_info()
                team_view = await crcon.get_team_view()
            except CRCONError as e:
                log.warning(f"[server_status] CRCON error: {e}")
                return

            from config import CRCON_URL
            embed = build_server_status_embed(info, {}, [], crcon_url=CRCON_URL)

            for row in configs:
                guild_id    = row["guild_id"]
                channel_id  = row["server_status_channel_id"]
                message_id  = row["server_status_message_id"]

                channel = bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(channel_id)
                    except discord.HTTPException:
                        continue

                by_team   = (info or {}).get("player_count_by_team") or {}
                team_embeds = build_team_view_embeds(
                    team_view or {},
                    allied=by_team.get("allied", 0),
                    axis=by_team.get("axis", 0),
                )
                embeds = [embed] + team_embeds

                if message_id:
                    try:
                        msg = await channel.fetch_message(message_id)
                        await msg.edit(embeds=embeds)
                        continue
                    except discord.NotFound:
                        pass
                    except discord.HTTPException as e:
                        log.warning(f"[server_status] Error editando mensaje: {e}")
                        continue

                # Si no hay mensaje guardado o fue borrado, creamos uno nuevo
                try:
                    new_msg = await channel.send(embeds=embeds)
                    async with pool.acquire() as conn:
                        await db_guild.set_server_status_message_id(conn, guild_id, new_msg.id)
                except discord.HTTPException as e:
                    log.warning(f"[server_status] Error enviando mensaje: {e}")

        except Exception as e:
            log.error(f"[server_status] Error en loop: {e}", exc_info=True)

    @server_status_loop.before_loop
    async def before():
        await bot.wait_until_ready()

    server_status_loop.start()
    return server_status_loop