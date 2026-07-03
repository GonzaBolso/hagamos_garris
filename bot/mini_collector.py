"""
mini_collector.py — Versión liviana del collector para uso puntual del bot.
Se usa desde snapshot_task.py cuando se detecta que la partida en curso
ya terminó y se quiere procesar de inmediato sin esperar al collector standalone.
Es seguro correr en paralelo al collector: usa ON CONFLICT DO NOTHING.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("mini_collector")


def _parse_dt(s: str):
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=-3)))
    return dt.astimezone(timezone.utc)


async def collect_new_matches(crcon_client, pool, max_pages: int = 2) -> int:
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

                if await conn.fetchval(
                    "SELECT 1 FROM matches WHERE match_id = $1", match_id
                ):
                    continue

                map_info     = m.get("map") or {}
                map_name     = map_info.get("pretty_name") or map_info.get("id", "?")
                result_info  = m.get("result") or {}
                allied_score = result_info.get("allied")
                axis_score   = result_info.get("axis")

                try:
                    detail  = await crcon_client.get_map_scoreboard(int(match_id))
                    players = (detail or {}).get("player_stats") or []
                except Exception as e:
                    log.warning(f"  No se pudieron obtener stats de partida {match_id}: {e}")
                    players = []

                match_start = _parse_dt((detail or {}).get("start"))
                match_end   = _parse_dt((detail or {}).get("end"))
                if not match_start:
                    continue

                await conn.execute(
                    """
                    INSERT INTO matches (match_id, map_name, start_time, end_time, allied_score, axis_score)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT DO NOTHING
                    """,
                    match_id, map_name, match_start, match_end, allied_score, axis_score,
                )

                name_to_id = {
                    p2.get("player", ""): p2.get("player_id", "")
                    for p2 in players
                    if p2.get("player_id") and p2.get("player")
                }

                for p in players:
                    steam_id = p.get("player_id", "")
                    if not steam_id or int(p.get("time_seconds") or 0) <= 0:
                        continue

                    most_killed_ids = {
                        name_to_id[name]: count
                        for name, count in (p.get("most_killed") or {}).items()
                        if name in name_to_id
                    }
                    death_by_ids = {
                        name_to_id[name]: count
                        for name, count in (p.get("death_by") or {}).items()
                        if name in name_to_id
                    }

                    await conn.execute(
                        """
                        INSERT INTO match_player_stats
                            (match_id, steam_id, player_name, kills, deaths, teamkills,
                             combat_score, offense_score, defense_score, support_score, time_seconds,
                             kills_by_type, deaths_by_type, weapons, death_by_weapons,
                             most_killed, death_by, most_killed_ids, death_by_ids)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                        ON CONFLICT (match_id, steam_id) DO NOTHING
                        """,
                        match_id, steam_id, p.get("player", ""),
                        int(p.get("kills") or 0), int(p.get("deaths") or 0),
                        int(p.get("teamkills") or 0), int(p.get("combat") or 0),
                        int(p.get("offense") or 0), int(p.get("defense") or 0),
                        int(p.get("support") or 0), int(p.get("time_seconds") or 0),
                        json.dumps(p.get("kills_by_type") or {}),
                        json.dumps(p.get("deaths_by_type") or {}),
                        json.dumps(p.get("weapons") or {}),
                        json.dumps(p.get("death_by_weapons") or {}),
                        json.dumps(p.get("most_killed") or {}),
                        json.dumps(p.get("death_by") or {}),
                        json.dumps(most_killed_ids),
                        json.dumps(death_by_ids),
                    )

                log.info(f"  [mini_collector] Nueva: [{match_id}] {map_name}")
                new_count += 1

        if new_count == 0:
            break

    return new_count