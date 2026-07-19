"""
challenge_close_task.py
Tarea que corre dentro del bot (discord.ext.tasks), revisando cada minuto
si el collector marcó algún desafío como recién cerrado
(pending_close_notification = TRUE). Si encuentra alguno, manda al canal
de desafíos configurado (challenge_channel_id) la misma "foto final" que
se ve con /hll desafio progreso, y apaga la marca.

El cierre en sí (detectar que la partida terminó, o que venció la
fecha_fin) lo hace el collector — esta tarea solo se encarga de la
notificación a Discord, reusando la lógica de formato que ya vive en
commands/challenges.py (build_progress_embed).
"""
import logging

import discord
from discord.ext import tasks

from commands.challenges import build_progress_embed

log = logging.getLogger("challenge_close_task")

CHECK_INTERVAL_MINUTES = 1


async def _notify_closed_challenges(bot, pool):
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            """
            SELECT c.id, c.guild_id, c.name, gc.challenge_channel_id
            FROM challenges c
            JOIN guild_config gc ON gc.guild_id = c.guild_id
            WHERE c.pending_close_notification = TRUE
              AND gc.challenge_channel_id IS NOT NULL
            """
        )

    if not pending:
        return

    for row in pending:
        challenge_id = row["id"]
        guild_id = row["guild_id"]
        channel_id = row["challenge_channel_id"]

        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                log.warning(f"  No pude resolver el canal {channel_id} (guild {guild_id})")
                continue

        embed, challenge = await build_progress_embed(pool, challenge_id, guild_id)

        try:
            if embed is not None:
                await channel.send(content="🏁 **Desafío finalizado**", embed=embed)
            else:
                nombre = challenge["name"] if challenge else f"#{challenge_id}"
                await channel.send(
                    f"🏁 **Desafío finalizado** — #{challenge_id} {nombre}\n"
                    f"_No hubo progreso registrado._"
                )
            log.info(f"  Notificación de cierre enviada: desafío #{challenge_id} (guild {guild_id})")
        except discord.HTTPException as e:
            log.error(f"  Error enviando notificación de cierre del desafío #{challenge_id}: {e}")
            continue  # no apagamos la marca si falló el envío, para reintentar

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE challenges SET pending_close_notification = FALSE WHERE id = $1",
                challenge_id
            )

        # Mensaje in-game a los jugadores que completaron el desafío
        try:
            async with pool.acquire() as conn:
                completados = await conn.fetch(
                    """
                    SELECT cp.steam_id, lp.discord_id, MAX(p.player_name) AS player_name
                    FROM challenge_progress cp
                    LEFT JOIN linked_players lp ON lp.steam_id = cp.steam_id
                    LEFT JOIN players p ON p.steam_id = cp.steam_id
                    WHERE cp.challenge_id = $1 AND cp.completed = TRUE
                    GROUP BY cp.steam_id, lp.discord_id
                    """,
                    challenge_id
                )
            nombre = challenge["name"] if challenge else f"#{challenge_id}"
            for p in completados:
                try:
                    from api.crcon import crcon
                    await crcon.message_player(
                        player_id=p["steam_id"],
                        player_name=p["player_name"] or p["steam_id"],
                        message=f"[Desafio #{challenge_id}] {nombre}\nLo completaste! Felicitaciones!"
                    )
                except Exception:
                    pass
        except Exception as e:
            log.warning(f"  No se pudo mandar mensaje in-game de completado: {e}")


def setup_challenge_close_task(bot, pool):
    """
    Registra la tarea de loop. Llamar una vez al iniciar el bot, ej:
    setup_challenge_close_task(bot, pool).start()
    """
    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def challenge_close_loop():
        try:
            await _notify_closed_challenges(bot, pool)
        except Exception as e:
            log.error(f"Error en challenge_close_loop: {e}", exc_info=True)
            await bot._send_status(f"⚠️ **Error en bot** (challenge close loop)\n```{type(e).__name__}: {e}```")

    return challenge_close_loop