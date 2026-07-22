"""
seed_notify_task.py — Monitorea el player count y avisa cuando el servidor
llega al umbral configurado para seedear.
"""
import logging

import discord
from discord.ext import tasks

from api import crcon, CRCONError
from db import guild as db_guild

log = logging.getLogger(__name__)


def setup_seed_notify_task(bot, pool):

    @tasks.loop(seconds=60)
    async def seed_notify_loop():
        try:
            async with pool.acquire() as conn:
                configs = await conn.fetch(
                    """SELECT guild_id, seed_role_id, seed_channel_id,
                              seed_threshold, seed_last_notified
                       FROM guild_config
                       WHERE seed_channel_id IS NOT NULL
                         AND seed_threshold IS NOT NULL"""
                )
            if not configs:
                return

            try:
                info = await crcon.get_public_info()
            except CRCONError as e:
                log.warning(f"[seed] CRCON error: {e}")
                return

            player_count = (info or {}).get("player_count", 0)
            max_players  = (info or {}).get("max_player_count", 100)

            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo
            TZ_UY = ZoneInfo("America/Montevideo")
            now_uy = datetime.now(TZ_UY)

            for row in configs:
                guild_id    = row["guild_id"]
                threshold   = row["seed_threshold"]
                last_ts     = row["seed_last_notified"]
                channel_id  = row["seed_channel_id"]
                role_id     = row["seed_role_id"]

                # Solo notificar si llegó al umbral
                if player_count < threshold:
                    continue

                # Solo notificar entre las 5:00 y las 23:59 hora UY
                if not (5 <= now_uy.hour <= 23):
                    continue

                # No notificar si ya se mandó hoy después de las 5am UY
                if last_ts:
                    last_uy = last_ts.astimezone(TZ_UY)
                    # Misma fecha calendario Y enviado después de las 5am
                    if last_uy.date() == now_uy.date() and last_uy.hour >= 5:
                        continue

                # Cruzó el umbral — mandar notificación
                channel = bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(channel_id)
                    except discord.HTTPException:
                        continue

                try:
                    players = await crcon.get_players()
                    player_names = [p.get("name", "?") for p in (players or [])[:20]]
                    players_txt = ", ".join(player_names)
                    if len(players or []) > 20:
                        players_txt += f" +{len(players)-20} más"
                except Exception:
                    players_txt = ""

                # Obtener canal de status para el link
                async with pool.acquire() as conn:
                    gc = await conn.fetchrow(
                        "SELECT server_status_channel_id FROM guild_config WHERE guild_id = $1",
                        guild_id
                    )
                status_channel_id = (gc or {}).get("server_status_channel_id")
                status_mention = f"<#{status_channel_id}>" if status_channel_id else ""

                role_mention = f"<@&{role_id}>" if role_id else ""
                msg = (
                    f"🪖 **¡EL SERVIDOR ESTÁ SEEDEANDO!** {role_mention}\n\n"
                    f"**{player_count}/{max_players} jugadores** conectados.\n"
                    f"¡Unite y ayudá a llenar el servidor!\n"
                )
                if players_txt:
                    msg += f"\n👥 Conectados: {players_txt}\n"
                if status_mention:
                    msg += f"\n📺 Info en vivo: {status_mention}"
                try:
                    await channel.send(msg)
                    async with pool.acquire() as conn:
                        await db_guild.set_seed_last_notified(conn, guild_id)
                    log.info(f"[seed] Notificación enviada: {player_count}/{max_players} jugadores")
                except discord.HTTPException as e:
                    log.warning(f"[seed] Error enviando notificación: {e}")

        except Exception as e:
            log.error(f"[seed] Error en loop: {e}", exc_info=True)

    @seed_notify_loop.before_loop
    async def before():
        await bot.wait_until_ready()

    seed_notify_loop.start()
    return seed_notify_loop