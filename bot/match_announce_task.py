"""
match_announce_task.py — Anuncia en el canal de seed cuando arranca y cuando
termina una partida en el servidor público (server 1). El anuncio de fin de
partida incluye resultado, mapa y el mejor jugador de cada categoría (combate/
ataque/defensa/apoyo) para cada lado.

Detecta MATCH START / MATCH ENDED via get_historical_logs, mismo mecanismo que
auto_message_task.py — pero con su propio cursor, independiente del de esa
tarea (no se acopla mensajes in-game con anuncios de Discord).

El resultado y el mapa salen directo del texto del log, sin depender de nada
más. El desglose de MVPs por categoría necesita que la partida ya esté
procesada en match_player_stats — se dispara un mini-collect puntual
(mini_collector.py, mismo mecanismo que usa snapshot_task.py) y se reintenta
por unos minutos; si nunca llega, el resultado ya se mandó igual, solo se
omite el desglose.
"""
import logging
import re
from datetime import datetime, timezone

import discord
from discord.ext import tasks

from db import matches as db_matches
from mini_collector import collect_new_matches

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
PUBLIC_SERVER_NUMBER = "1"  # server 2 es el de entrenamiento, se ignora
MATCH_EVENT_ACTIONS = ("MATCH START", "MATCH ENDED")

MVP_MAX_WAIT_MINUTES = 5

MATCH_START_RE = re.compile(r"MATCH START (.+)")
MATCH_ENDED_RE = re.compile(r"MATCH ENDED `(.+?)` ALLIED \((\d+) - (\d+)\) AXIS")

CATEGORY_LABELS = (
    ("combat",  "🔥 Combate"),
    ("offense", "⚔️ Ataque"),
    ("defense", "🛡️ Defensa"),
    ("support", "🤝 Apoyo"),
)


def _format_names(names: list) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " y " + names[-1]


def _format_side(top: dict, side_label: str) -> str:
    lines = []
    for key, label in CATEGORY_LABELS:
        entry = top.get(key)
        if entry:
            lines.append(f"{label}: **{_format_names(entry['player_names'])}** ({entry['value']} pts)")
        else:
            lines.append(f"{label}: sin datos")
    return f"**{side_label}**\n" + "\n".join(lines)


def build_mvp_message(top_players: dict, map_name: str) -> str:
    allies_txt = _format_side(top_players.get("allies") or {}, "🔵 Aliados")
    axis_txt   = _format_side(top_players.get("axis") or {}, "🔴 Eje")
    return f"⭐ **Lo mejor de {map_name}**\n\n{allies_txt}\n\n{axis_txt}"


async def _find_recent_closed_match_id(crcon_client, server_number: str = PUBLIC_SERVER_NUMBER):
    """Devuelve el match_id de la partida cerrada más reciente en el server público."""
    try:
        result = await crcon_client.get_scoreboard_maps(page=1, page_size=10)
    except Exception as e:
        log.warning(f"[match_announce] get_scoreboard_maps falló: {e}")
        return None

    maps = (result or {}).get("maps", [])
    candidates = [
        m for m in maps
        if str(m.get("server_number")) == str(server_number) and m.get("end")
    ]
    if not candidates:
        return None

    best = max(candidates, key=lambda m: m.get("id", 0))
    return str(best.get("id"))


def setup_match_announce_task(bot, pool, crcon_client):

    last_seen_id = {action: None for action in MATCH_EVENT_ACTIONS}  # None = todavía sin primear

    # Solo se seguí el MVP de la partida más reciente que terminó — si CRCON
    # nunca llega a indexar el scoreboard de una partida y ya arrancó/terminó
    # la siguiente, no tiene sentido seguir esperando la vieja (no hay forma
    # confiable de encontrarla igual, _find_recent_closed_match_id solo puede
    # dar la ÚLTIMA cerrada).
    # baseline_match_id: la última partida que CRCON ya tenía indexada en el
    # momento en que ésta terminó — solo un match_id DISTINTO a ese significa
    # que el scoreboard avanzó de verdad. Comparar contra un baseline propio
    # de cada evento (en vez de un cursor global) evita falsos positivos tras
    # un restart del bot, donde no sabríamos si "la última indexada" ya
    # corresponde a esta partida o a una completamente vieja.
    pending_mvp = {"map_name": None, "detected_at": None, "baseline_match_id": None}

    # Detector de "CRCON dejó de indexar partidas": evita mandar un warning
    # por cada partida que se descarta durante un corte prolongado del lado
    # de CRCON — se loguea una sola vez al detectarlo y una sola vez al
    # recuperarse, en vez de un warning repetido cada vez que se da por
    # vencido un intento de MVP.
    scoreboard_state = {"stuck": False}

    async def _get_announce_channels(conn):
        return await conn.fetch(
            """
            SELECT guild_id, seed_channel_id FROM guild_config
            WHERE seed_channel_id IS NOT NULL
              AND COALESCE(match_announce_activo, TRUE) = TRUE
            """
        )

    async def _send_to_all(rows, text: str):
        for row in rows:
            channel_id = row["seed_channel_id"]
            channel = bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    log.warning(f"[match_announce] No pude resolver el canal {channel_id}")
                    continue
            try:
                await channel.send(text)
            except discord.HTTPException as e:
                log.warning(f"[match_announce] Error enviando a canal {channel_id}: {e}")

    async def _handle_match_start(entry: dict):
        content = entry.get("content") or ""
        m = MATCH_START_RE.search(content)
        map_name = m.group(1).title() if m else "?"

        async with pool.acquire() as conn:
            channels = await _get_announce_channels(conn)
        await _send_to_all(channels, f"🏁 ¡Arrancó una nueva partida en **{map_name}**!")
        log.info(f"[match_announce] MATCH START: {map_name}")

    async def _handle_match_ended(entry: dict):
        content = entry.get("content") or ""
        m = MATCH_ENDED_RE.search(content)
        if not m:
            log.warning(f"[match_announce] No pude parsear MATCH ENDED: {content!r}")
            return

        map_name_raw, allied_score, axis_score = m.group(1), int(m.group(2)), int(m.group(3))
        map_name = map_name_raw.title()

        if allied_score == axis_score:
            # Un empate (2-2) suele ser el mapa llegando al límite de tiempo
            # sin que nadie capturara territorio real — no se anuncia.
            log.info(f"[match_announce] MATCH ENDED: {map_name} ({allied_score}-{axis_score}), empate, se omite el anuncio.")
            return

        if allied_score > axis_score:
            resultado = f"🏆 ¡Los **Aliados** se llevaron la victoria! ({allied_score} - {axis_score})"
        else:
            resultado = f"🏆 ¡El **Eje** se llevó la victoria! ({axis_score} - {allied_score})"

        async with pool.acquire() as conn:
            channels = await _get_announce_channels(conn)
        await _send_to_all(channels, f"🎖️ Terminó la partida en **{map_name}**\n{resultado}")
        log.info(f"[match_announce] MATCH ENDED: {map_name} ({allied_score}-{axis_score})")

        if pending_mvp["map_name"] is not None:
            log.info(
                f"[match_announce] Se descarta el MVP pendiente de '{pending_mvp['map_name']}' "
                f"(no llegó a resolverse antes de que terminara '{map_name}')."
            )

        pending_mvp["map_name"]         = map_name
        pending_mvp["detected_at"]      = datetime.now(timezone.utc)
        pending_mvp["baseline_match_id"] = await _find_recent_closed_match_id(crcon_client)

    async def _process_pending_mvp():
        if pending_mvp["map_name"] is None:
            return

        try:
            await collect_new_matches(crcon_client, pool)
        except Exception as e:
            log.warning(f"[match_announce] mini-collect falló: {e}")

        match_id = await _find_recent_closed_match_id(crcon_client)

        if match_id is not None and match_id != pending_mvp["baseline_match_id"]:
            # El scoreboard avanzó más allá de donde estaba cuando terminó esta
            # partida -> CRCON la indexó. Se resuelve acá pase lo que pase (con
            # o sin datos de equipo), nunca es un problema de CRCON trabado.
            if scoreboard_state["stuck"]:
                log.info(f"[match_announce] El scoreboard de CRCON volvió a actualizarse (partida {match_id}).")
                scoreboard_state["stuck"] = False

            async with pool.acquire() as conn:
                top_players = await db_matches.get_match_top_players(conn, match_id)

            if top_players.get("allies") or top_players.get("axis"):
                async with pool.acquire() as conn:
                    channels = await _get_announce_channels(conn)
                await _send_to_all(channels, build_mvp_message(top_players, pending_mvp["map_name"]))
                log.info(f"[match_announce] MVPs enviados para partida {match_id} ({pending_mvp['map_name']})")
            else:
                log.info(
                    f"[match_announce] Partida {match_id} ({pending_mvp['map_name']}) indexada, pero sin "
                    f"jugadores con equipo determinado (poca actividad) — se omite el desglose de MVPs."
                )

            pending_mvp["map_name"] = None
            return

        elapsed_min = (datetime.now(timezone.utc) - pending_mvp["detected_at"]).total_seconds() / 60
        if elapsed_min < MVP_MAX_WAIT_MINUTES:
            return  # sigue esperando, nada que loguear todavía

        if not scoreboard_state["stuck"]:
            log.warning(
                f"[match_announce] CRCON no está indexando partidas nuevas del server público "
                f"(sin novedades tras {elapsed_min:.0f} min esperando '{pending_mvp['map_name']}'); "
                f"se van a omitir los MVPs hasta que se destrabe, sin repetir este aviso."
            )
            scoreboard_state["stuck"] = True
        else:
            log.info(
                f"[match_announce] Se descarta el MVP de '{pending_mvp['map_name']}' "
                f"(CRCON sigue sin indexar partidas nuevas)."
            )

        pending_mvp["map_name"] = None

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def match_announce_loop():
        try:
            for action in MATCH_EVENT_ACTIONS:
                try:
                    entries = await crcon_client.get_historical_logs(action=action, limit=10)
                except Exception as e:
                    log.warning(f"[match_announce] No se pudo obtener historial ({action}): {e}")
                    continue

                entries = [e for e in entries if str(e.get("server")) == PUBLIC_SERVER_NUMBER]
                if not entries:
                    continue

                max_id = max(e.get("id", 0) for e in entries)
                seen_before = last_seen_id[action]

                if seen_before is None:
                    # primer ciclo: solo primea el cursor, no dispara retroactivamente
                    last_seen_id[action] = max_id
                    continue

                nuevos = sorted(
                    (e for e in entries if e.get("id", 0) > seen_before),
                    key=lambda e: e.get("id", 0),
                )
                if not nuevos:
                    continue

                last_seen_id[action] = max_id

                for entry in nuevos:
                    if action == "MATCH START":
                        await _handle_match_start(entry)
                    else:
                        await _handle_match_ended(entry)

            await _process_pending_mvp()

        except Exception as e:
            log.error(f"[match_announce] Error en loop: {e}", exc_info=True)
            await bot._send_status(f"⚠️ **Error en bot** (match announce loop)\n```{type(e).__name__}: {e}```")

    @match_announce_loop.before_loop
    async def before():
        await bot.wait_until_ready()

    match_announce_loop.start()
    return match_announce_loop
