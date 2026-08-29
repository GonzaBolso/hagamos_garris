"""
auto_message_task.py — Manda mensajes automáticos a todos los jugadores conectados.
Modo configurable por guild via /hlladmin mensajes subir (campo "modo" del JSON):
  - "evento":    dispara al INICIO y al FINAL de cada partida (MATCH START / MATCH ENDED).
  - "intervalo": dispara cada N minutos (intervalo_minutos), como el comportamiento original.
"""
import json
import logging
import random
from datetime import datetime, timezone, timedelta

from discord.ext import tasks

from api import crcon, CRCONError

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30  # cada cuánto se chequea (logs de CRCON y vencimiento de intervalos)
MATCH_EVENT_ACTIONS = ("MATCH START", "MATCH ENDED")


def _event_key(ev: dict) -> str:
    return f"{ev.get('action')}|{ev.get('timestamp_ms')}|{ev.get('message')}"


def _active_texts(mensajes) -> list:
    if isinstance(mensajes, str):
        mensajes = json.loads(mensajes)
    return [m["texto"] for m in (mensajes or []) if m.get("activo") and m.get("texto")]


def setup_auto_message_task(bot, pool):

    seen_keys = set()
    state = {"primed": False}  # primer ciclo: solo registra eventos existentes, no manda nada
    last_sent = {}  # guild_id -> datetime del último envío, solo para modo "intervalo"

    async def _broadcast(texto: str, players: list) -> int:
        enviados = 0
        for p in players:
            pid  = p.get("player_id") or p.get("steam_id")
            name = p.get("name", "")
            if not pid:
                continue
            try:
                await crcon.message_player(player_id=pid, player_name=name, message=texto)
                enviados += 1
            except Exception:
                pass
        return enviados

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def auto_message_loop():
        try:
            async with pool.acquire() as conn:
                configs = await conn.fetch(
                    "SELECT guild_id, activo, modo, intervalo_min, mensajes FROM auto_messages"
                )
            if not configs:
                return

            event_configs     = [r for r in configs if r["activo"] and (r["modo"] or "evento") == "evento"]
            intervalo_configs = [r for r in configs if r["activo"] and (r["modo"] or "evento") == "intervalo"]

            new_events = []
            if event_configs:
                for action in MATCH_EVENT_ACTIONS:
                    try:
                        events = await crcon.get_recent_logs(limit=20, action=action)
                    except Exception as e:
                        log.warning(f"[auto_msg] No se pudo obtener logs ({action}): {e}")
                        continue

                    for ev in events:
                        if ev.get("action") != action:
                            continue
                        key = _event_key(ev)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        if state["primed"]:
                            new_events.append((action, ev))

                state["primed"] = True

            now = datetime.now(timezone.utc)
            due_intervalo = []
            for row in intervalo_configs:
                intervalo = row["intervalo_min"] or 15
                last = last_sent.get(row["guild_id"])
                if not last or (now - last) >= timedelta(minutes=intervalo):
                    due_intervalo.append(row)

            if not new_events and not due_intervalo:
                return

            try:
                players = await crcon.get_players() or []
            except Exception as e:
                log.warning(f"[auto_msg] No se pudo obtener jugadores: {e}")
                return

            if not players:
                log.info("[auto_msg] Sin jugadores conectados, no se envía")
                return

            for action, ev in new_events:
                log.info(f"[auto_msg] Evento detectado: {action} — {ev.get('message', '')[:80]}")
                for row in event_configs:
                    activos = _active_texts(row["mensajes"])
                    if not activos:
                        continue
                    texto = random.choice(activos)
                    enviados = await _broadcast(texto, players)
                    log.info(f"[auto_msg] ({action}) mensaje enviado a {enviados} jugadores (guild {row['guild_id']})")

            for row in due_intervalo:
                activos = _active_texts(row["mensajes"])
                if not activos:
                    log.info(f"[auto_msg] Sin mensajes activos configurados (guild {row['guild_id']})")
                    continue
                texto = random.choice(activos)
                enviados = await _broadcast(texto, players)
                last_sent[row["guild_id"]] = now
                log.info(f"[auto_msg] (intervalo) mensaje enviado a {enviados} jugadores (guild {row['guild_id']})")

        except Exception as e:
            log.error(f"[auto_msg] Error en loop: {e}", exc_info=True)

    @auto_message_loop.before_loop
    async def before():
        await bot.wait_until_ready()

    auto_message_loop.start()
    return auto_message_loop
