"""
mini_collector.py
Versión liviana del collector, pensada para ser invocada PUNTUALMENTE por el
bot de Discord (no corre en loop). Usa el mismo cliente CRCON que ya tiene
el bot (api/crcon.py) en vez de una sesión aiohttp aparte.

Se usa desde snapshot_task.py: cuando se detecta que el mapa que estaba en
curso al momento del snapshot ya terminó, se llama a collect_new_matches()
para procesar esa partida de inmediato, en vez de esperar al próximo ciclo
del collector standalone (que corre cada 10-30 min en su propio contenedor).

Es seguro correr esto en paralelo al collector standalone: ambos insertan
con ON CONFLICT DO NOTHING sobre las mismas tablas, así que no hay
duplicados ni carreras problemáticas si llegan a pisarse.
"""
import logging
from datetime import datetime

log = logging.getLogger("mini_collector")


def _parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s)


async def collect_new_matches(crcon_client, pool, max_pages: int = 2) -> int:
    """
    Revisa get_scoreboard_maps (1-2 páginas, las más recientes) y guarda en
    la base cualquier partida que todavía no exista. Devuelve cuántas
    partidas nuevas se guardaron.
    """
    new_count = 0

    for page in range(1, max_pages + 1):
        try:
            result = await crcon_client.get_scoreboard_maps(page=page, page_size=50)
        except Exception as e:
            log.warning(f"  get_scoreboard_maps(page={page}) falló: {e}")
            break

        maps = (result or {}).get("maps", [])
        if not maps:
            break

        async with pool.acquire() as conn:
            for m in maps:
                match_id = str(m.get("id", ""))
                if not match_id:
                    continue

                exists = await conn.fetchval(
                    "SELECT 1 FROM matches WHERE match_id = $1", match_id
                )
                if exists:
                    continue

                map_info     = m.get("map") or {}
                map_name     = map_info.get("pretty_name") or map_info.get("id", "?")
                result_info  = m.get("result") or {}
                allied_score = result_info.get("allied")
                axis_score   = result_info.get("axis")

                await conn.execute(
                    """
                    INSERT INTO matches (match_id, map_name, start_time, end_time, allied_score, axis_score)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT DO NOTHING
                    """,
                    match_id,
                    map_name,
                    _parse_dt(m.get("start")),
                    _parse_dt(m.get("end")),
                    allied_score,
                    axis_score,
                )

                try:
                    detail = await crcon_client.get_map_scoreboard(int(match_id))
                    players = (detail or {}).get("player_stats") or []
                except Exception as e:
                    log.warning(f"  No se pudieron obtener stats de partida {match_id}: {e}")
                    players = []

                for p in players:
                    steam_id = p.get("player_id", "")
                    if not steam_id:
                        continue

                    time_sec = int(p.get("time_seconds") or 0)
                    if time_sec <= 0:
                        continue

                    await conn.execute(
                        """
                        INSERT INTO match_player_stats
                            (match_id, steam_id, player_name, kills, deaths, teamkills,
                             combat_score, offense_score, defense_score, support_score, time_seconds)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        ON CONFLICT (match_id, steam_id) DO NOTHING
                        """,
                        match_id,
                        steam_id,
                        p.get("player", ""),
                        int(p.get("kills") or 0),
                        int(p.get("deaths") or 0),
                        int(p.get("teamkills") or 0),
                        int(p.get("combat") or 0),
                        int(p.get("offense") or 0),
                        int(p.get("defense") or 0),
                        int(p.get("support") or 0),
                        time_sec,
                    )

                log.info(f"  [mini_collector] Nueva: [{match_id}] {map_name}")
                new_count += 1

        # Si esta página no trajo nada nuevo, las páginas más viejas tampoco
        # (vienen ordenadas de más nueva a más vieja desde CRCON).
        if new_count == 0:
            break

    return new_count