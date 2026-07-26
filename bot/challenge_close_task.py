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
        vip_dias = 0  # se carga más abajo

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

        # Dar VIP a los que completaron (si el desafío tiene premio_vip_dias)
        try:
            async with pool.acquire() as conn:
                ch_data = await conn.fetchrow(
                    "SELECT name, premio_vip_dias FROM challenges WHERE id = $1",
                    challenge_id
                )
                vip_dias = (ch_data or {}).get("premio_vip_dias") or 0

            if vip_dias > 0:
                async with pool.acquire() as conn:
                    completados_vip = await conn.fetch(
                        """
                        SELECT cp.steam_id, COALESCE(MAX(p.player_name), cp.steam_id) AS player_name
                        FROM challenge_progress cp
                        JOIN linked_players lp ON lp.steam_id = cp.steam_id
                        LEFT JOIN players p ON p.steam_id = cp.steam_id
                        WHERE cp.challenge_id = $1 AND cp.completed = TRUE
                        GROUP BY cp.steam_id
                        """,
                        challenge_id
                    )

                if completados_vip:
                    from api.crcon import crcon
                    from datetime import datetime, timezone, timedelta
                    import json as _json

                    # Obtener VIPs actuales para extender si ya tienen
                    current_vips = {}
                    try:
                        vip_list = await crcon.get_vip_ids() or []
                        for v in vip_list:
                            current_vips[v.get("player_id")] = v.get("expiration")
                    except Exception:
                        pass

                    nombre_desafio = (ch_data or {}).get("name", f"#{challenge_id}")

                    for p in completados_vip:
                        sid = p["steam_id"]
                        try:
                            existing_exp = current_vips.get(sid)

                            # VIP permanente (null o "never") — no tocar
                            if existing_exp in (None, "never", ""):
                                if existing_exp is None and sid not in current_vips:
                                    # No tiene VIP — dar desde ahora
                                    base = datetime.now(timezone.utc)
                                else:
                                    # Tiene VIP permanente — no hacer nada
                                    log.info(f"  VIP permanente detectado para {p['player_name']}, no se modifica")
                                    continue

                            else:
                                # Tiene VIP con fecha — extender desde esa fecha
                                try:
                                    base = datetime.fromisoformat(existing_exp.replace("Z", "+00:00"))
                                    if base < datetime.now(timezone.utc):
                                        base = datetime.now(timezone.utc)
                                except Exception:
                                    base = datetime.now(timezone.utc)

                            new_exp = (base + timedelta(days=vip_dias)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                            desc    = f"-Desafio- {p['player_name']}"

                            ok = await crcon.add_vip(
                                player_id=sid,
                                player_name=p["player_name"],
                                expiration=new_exp,
                                description=desc,
                            )
                            if ok:
                                log.info(f"  VIP +{vip_dias}d dado a {p['player_name']} (#{challenge_id})")
                            else:
                                log.warning(f"  add_vip devolvió false para {sid}")
                        except Exception as e:
                            log.warning(f"  Error dando VIP a {sid}: {e}")
        except Exception as e:
            log.warning(f"  Error procesando VIP del desafío #{challenge_id}: {e}")
        try:
            async with pool.acquire() as conn:
                completados = await conn.fetch(
                    """
                    SELECT cp.steam_id, MAX(p.player_name) AS player_name
                    FROM challenge_progress cp
                    JOIN linked_players lp ON lp.steam_id = cp.steam_id
                    LEFT JOIN players p ON p.steam_id = cp.steam_id
                    WHERE cp.challenge_id = $1 AND cp.completed = TRUE
                    GROUP BY cp.steam_id
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
                        + (f"\nPremio: +{vip_dias} dias de VIP!" if vip_dias > 0 else "")
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