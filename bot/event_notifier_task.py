"""
event_notifier_task.py
Tarea que corre dentro del bot (discord.ext.tasks), revisando cada 20
segundos si el collector encoló algún evento destacado en
detected_events (notified = FALSE). Si encuentra alguno, lo manda al
canal de eventos configurado (eventos_channel_id) y marca notified=TRUE.

La detección en sí (ej: kill con arma de melee = "fakeo") la hace el
collector — esta tarea solo se encarga de la notificación a Discord,
mismo patrón que challenge_close_task.py.
"""
import logging

import discord
from discord.ext import tasks

log = logging.getLogger("event_notifier_task")

CHECK_INTERVAL_SECONDS = 20


async def _notify_pending_events(bot, pool):
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            """
            SELECT de.id, de.guild_id, de.message, gc.eventos_channel_id
            FROM detected_events de
            JOIN guild_config gc ON gc.guild_id = de.guild_id
            WHERE de.notified = FALSE
              AND gc.eventos_channel_id IS NOT NULL
            ORDER BY de.created_at ASC
            """
        )

    if not pending:
        return

    for row in pending:
        event_id = row["id"]
        guild_id = row["guild_id"]
        channel_id = row["eventos_channel_id"]

        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning(f"  No pude resolver el canal {channel_id} (guild {guild_id})")
                continue

        try:
            await channel.send(row["message"])
        except discord.HTTPException as e:
            log.error(f"  Error enviando evento #{event_id}: {e}")
            continue  # no marcamos notified si falló, para reintentar

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE detected_events SET notified = TRUE WHERE id = $1",
                event_id
            )


def setup_event_notifier_task(bot, pool):
    """
    Registra la tarea de loop. Llamar una vez al iniciar el bot, ej:
    setup_event_notifier_task(bot, pool).start()
    """
    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def event_notifier_loop():
        try:
            await _notify_pending_events(bot, pool)
        except Exception as e:
            log.error(f"Error en event_notifier_loop: {e}", exc_info=True)
            await bot._send_status(f"⚠️ **Error en bot** (event notifier loop)\n```{type(e).__name__}: {e}```")

    return event_notifier_loop