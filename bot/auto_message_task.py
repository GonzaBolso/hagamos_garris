"""
auto_message_task.py — Manda mensajes automáticos a todos los jugadores conectados.
El intervalo y los mensajes se configuran via /hlladmin mensajes subir.
"""
import logging
import random

from discord.ext import tasks

from api import crcon, CRCONError

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60  # chequea cada 60s, pero respeta el intervalo configurado


def setup_auto_message_task(bot, pool):

    last_sent = {"ts": None}  # timestamp del último mensaje enviado

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def auto_message_loop():
        try:
            async with pool.acquire() as conn:
                configs = await conn.fetch(
                    "SELECT guild_id, activo, intervalo_min, mensajes FROM auto_messages"
                )
            if not configs:
                return

            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)

            for row in configs:
                if not row["activo"]:
                    continue

                intervalo = row["intervalo_min"] or 15
                last = last_sent.get(row["guild_id"])
                if last and (now - last) < timedelta(minutes=intervalo):
                    continue

                mensajes = row["mensajes"] or []
                if isinstance(mensajes, str):
                    import json as _j
                    mensajes = _j.loads(mensajes)
                activos = [m["texto"] for m in mensajes if m.get("activo") and m.get("texto")]
                if not activos:
                    log.info(f"[auto_msg] Sin mensajes activos configurados")
                    continue

                # Elegir mensaje al azar
                texto = random.choice(activos)
                log.info(f"[auto_msg] Enviando mensaje: '{texto[:50]}{'...' if len(texto) > 50 else ''}'")

                # Obtener jugadores conectados
                try:
                    players = await crcon.get_players() or []
                except Exception as e:
                    log.warning(f"[auto_msg] No se pudo obtener jugadores: {e}")
                    continue

                if not players:
                    log.info(f"[auto_msg] Sin jugadores conectados, no se envía")
                    continue

                # Mandar a todos
                enviados = 0
                for p in players:
                    pid  = p.get("player_id") or p.get("steam_id")
                    name = p.get("name", "")
                    if not pid:
                        continue
                    try:
                        await crcon.message_player(
                            player_id=pid,
                            player_name=name,
                            message=texto,
                        )
                        enviados += 1
                    except Exception:
                        pass

                last_sent[row["guild_id"]] = now
                log.info(f"[auto_msg] Mensaje enviado a {enviados} jugadores")

        except Exception as e:
            log.error(f"[auto_msg] Error en loop: {e}", exc_info=True)

    @auto_message_loop.before_loop
    async def before():
        await bot.wait_until_ready()

    auto_message_loop.start()
    return auto_message_loop