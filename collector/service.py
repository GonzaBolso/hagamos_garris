import asyncio
"""service.py — Lógica de negocio del collector.
Orquesta llamadas a crcon.py (HTTP) y db.py (SQL).
No sabe nada de Discord ni de loops.
"""
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
import asyncpg

import config
import crcon
import db

log = logging.getLogger(__name__)


def parse_dt(s: str):
    """
    Parsea timestamps de CRCON a datetime UTC.
    - get_map_scoreboard: vienen con offset explícito (+00:00) → ya es UTC.
    - get_scoreboard_maps / logs: vienen naive en hora local del servidor
      (UTC-3) → se interpreta con ese offset y se convierte a UTC.
    """
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=-3)))
    return dt.astimezone(timezone.utc)


def make_event_key(ev: dict) -> str:
    """Clave única por evento para deduplicar entre consultas sucesivas."""
    return (
        f"{ev.get('timestamp_ms')}|{ev.get('player_id_1')}|"
        f"{ev.get('player_id_2')}|{ev.get('weapon')}"
    )


def aggregate_live_stats_by_player(live_result: dict) -> dict:
    """
    Convierte la respuesta de get_live_game_stats en:
    steam_id -> {kills, deaths, combat, offense, defense, support,
                 kills_by_type, player_name}
    """
    out = {}
    for p in (live_result or {}).get("stats", []):
        steam_id = p.get("player_id")
        if not steam_id:
            continue
        out[steam_id] = {
            "player_name":        p.get("player", ""),
            "kills":              int(p.get("kills") or 0),
            "deaths":             int(p.get("deaths") or 0),
            "combat":             int(p.get("combat") or 0),
            "offense":            int(p.get("offense") or 0),
            "defense":            int(p.get("defense") or 0),
            "support":            int(p.get("support") or 0),
            "kills_by_type":      p.get("kills_by_type") or {},
            "vehicles_destroyed": int(p.get("vehicles_destroyed") or 0),
        }
    return out


# Mapeo métrica -> campo en live_by_player
LIVE_METRIC_FIELD = {
    "kills":   "kills",
    "combat":  "combat",
    "offense": "offense",
    "defense": "defense",
    "support": "support",
}

# Estado del cursor de kills en vivo — se resetea al cambiar de partida
_live_kills_state = {"map_start": None, "kills_by_player": {}, "seen_keys": set()}

# Estado del detector de eventos destacados
_event_detector_state = {"map_start": None, "seen_keys": set()}


async def update_live_kills_state(session: aiohttp.ClientSession,
                                   map_start: int, map_start_dt) -> dict:
    """
    Mantiene _live_kills_state al día para la partida en curso.
    Devuelve kills_by_player: steam_id -> {by_weapon, by_victim, by_type}.
    """
    if _live_kills_state["map_start"] != map_start:
        _live_kills_state["map_start"] = map_start
        _live_kills_state["kills_by_player"] = {}
        _live_kills_state["seen_keys"] = set()

    try:
        events = await crcon.fetch_recent_logs(session, limit=2000, action="KILL")
    except Exception as e:
        log.warning(f"  [live] get_recent_logs falló: {e}")
        return _live_kills_state["kills_by_player"]

    seen_keys = _live_kills_state.setdefault("seen_keys", set())

    for ev in events:
        if ev.get("action") != "KILL":
            continue
        killer_id = ev.get("player_id_1")
        victim_id = ev.get("player_id_2")
        weapon    = ev.get("weapon")
        if not killer_id or not victim_id:
            continue

        ts_ms = ev.get("timestamp_ms")
        if ts_ms is not None and map_start_dt is not None:
            if ts_ms < map_start_dt.timestamp() * 1000:
                continue  # kill de partida anterior

        key = make_event_key(ev)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        state = _live_kills_state["kills_by_player"].setdefault(
            killer_id,
            {"player_name": None, "by_weapon": {}, "by_victim": {}, "by_type": {}}
        )
        if ev.get("player_name_1"):
            state["player_name"] = ev["player_name_1"]
        if weapon:
            state["by_weapon"][weapon] = state["by_weapon"].get(weapon, 0) + 1
        state["by_victim"][victim_id] = state["by_victim"].get(victim_id, 0) + 1
        kill_type = ev.get("kill_type") or ev.get("type_1")
        if kill_type:
            state["by_type"][kill_type] = state["by_type"].get(kill_type, 0) + 1

    return _live_kills_state["kills_by_player"]


async def compute_combined_metric_values(conn: asyncpg.Connection,
                                          metric: str,
                                          live_by_player: dict,
                                          match_ids: list = None,
                                          start_date=None, end_date=None,
                                          param: str = None,
                                          live_kills_by_player: dict = None) -> list:
    """
    Como fetch_metric_values pero sumando el aporte de la partida en vivo.
    Devuelve [{steam_id, player_name, value}].
    """
    live_kills_by_player = live_kills_by_player or {}

    if metric in db.JSONB_COLUMN:
        if not param:
            return []
        jsonb_col  = db.JSONB_COLUMN[metric]
        if match_ids is not None:
            where_clause = "mps.match_id = ANY($1::varchar[])"
            params_closed = [match_ids, param]
            param_idx = 2
        else:
            where_clause = "m.start_time BETWEEN $1 AND $2"
            params_closed = [start_date, end_date, param]
            param_idx = 3

        closed = await conn.fetch(
            f"""
            SELECT mps.steam_id,
                   COALESCE(p.player_name, MAX(mps.player_name)) AS player_name,
                   COALESCE(SUM((mps.{jsonb_col}->>${param_idx})::int), 0) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            LEFT JOIN players p ON p.steam_id = mps.steam_id
            WHERE {where_clause}
              AND mps.{jsonb_col} ? ${param_idx}
            GROUP BY mps.steam_id, p.player_name
            """,
            *params_closed
        )
        closed_by_player = {r["steam_id"]: (r["value"] or 0) for r in closed}
        names_by_player  = {r["steam_id"]: r["player_name"] for r in closed}

        if metric == "kills_type":
            live_field  = "kills_by_type"
            live_source = live_by_player
        else:
            live_field  = "by_weapon" if metric == "kills_weapon" else "by_victim"
            live_source = live_kills_by_player

        all_steam_ids = set(closed_by_player) | set(live_source)
        return [
            {
                "steam_id":   sid,
                "player_name": (
                    names_by_player.get(sid)
                    or live_source.get(sid, {}).get("player_name")
                    or sid
                ),
                "value": closed_by_player.get(sid, 0)
                         + live_source.get(sid, {}).get(live_field, {}).get(param, 0),
            }
            for sid in all_steam_ids
        ]

    if metric == "kd_ratio":
        closed_kills  = await db.fetch_metric_values(conn, "kills",  match_ids, start_date, end_date)
        closed_deaths = await db.fetch_closed_deaths(conn, match_ids, start_date, end_date)
        kills_by  = {r["steam_id"]: (r["value"] or 0) for r in closed_kills}
        deaths_by = {r["steam_id"]: (r["value"] or 0) for r in closed_deaths}
        names     = {r["steam_id"]: r["player_name"] for r in closed_kills}
        names.update({r["steam_id"]: r["player_name"] for r in closed_deaths})
        all_ids   = set(kills_by) | set(deaths_by) | set(live_by_player)
        return [
            {
                "steam_id":   sid,
                "player_name": names.get(sid) or live_by_player.get(sid, {}).get("player_name"),
                "value": (lambda k, d: float(k) if d == 0 else round(k / d, 2))(
                    kills_by.get(sid, 0)  + live_by_player.get(sid, {}).get("kills", 0),
                    deaths_by.get(sid, 0) + live_by_player.get(sid, {}).get("deaths", 0),
                ),
            }
            for sid in all_ids
        ]

    if metric == "matches":
        closed = await db.fetch_metric_values(conn, "matches", match_ids, start_date, end_date)
        closed_by = {r["steam_id"]: (r["value"] or 0) for r in closed}
        names     = {r["steam_id"]: r["player_name"] for r in closed}
        all_ids   = set(closed_by) | set(live_by_player)
        return [
            {
                "steam_id":   sid,
                "player_name": names.get(sid) or live_by_player.get(sid, {}).get("player_name"),
                "value": closed_by.get(sid, 0) + (1 if sid in live_by_player else 0),
            }
            for sid in all_ids
        ]

    live_field = LIVE_METRIC_FIELD.get(metric)
    closed     = await db.fetch_metric_values(conn, metric, match_ids, start_date, end_date)
    closed_by  = {r["steam_id"]: (r["value"] or 0) for r in closed}
    names      = {r["steam_id"]: r["player_name"] for r in closed}
    all_ids    = set(closed_by) | set(live_by_player)
    return [
        {
            "steam_id":   sid,
            "player_name": names.get(sid) or live_by_player.get(sid, {}).get("player_name"),
            "value": closed_by.get(sid, 0)
                     + (live_by_player.get(sid, {}).get(live_field, 0) if live_field else 0),
        }
        for sid in all_ids
    ]


async def process_maps(pool: asyncpg.Pool, session: aiohttp.ClientSession,
                        maps: list, live_map_start_epoch: float = None) -> int:
    """
    Procesa la lista de partidas de get_scoreboard_maps.
    Salta la partida en vivo (no cerró todavía) y las que ya están en BD.
    """
    new_count = 0

    async with pool.acquire() as conn:
        for m in maps:
            match_id = str(m.get("id", ""))
            if not match_id:
                continue

            if await db.match_exists(conn, match_id):
                continue

            # Saltar la partida en curso
            if live_map_start_epoch is not None:
                m_start_raw = m.get("start")
                if m_start_raw:
                    try:
                        naive = datetime.fromisoformat(m_start_raw)
                        if naive.tzinfo is None:
                            naive = naive.replace(tzinfo=timezone(timedelta(hours=-3)))
                        if abs(naive.timestamp() - live_map_start_epoch) < 5:
                            continue
                    except Exception:
                        pass

            map_info     = m.get("map") or {}
            map_name     = map_info.get("pretty_name") or map_info.get("id", "?")
            result       = m.get("result") or {}
            allied_score = result.get("allied")
            axis_score   = result.get("axis")

            try:
                detail  = await crcon.fetch_map_scoreboard(session, int(match_id))
                players = detail.get("player_stats") or []
            except Exception as e:
                log.warning(f"  No se pudieron obtener stats de partida {match_id}: {e}, reintentando en el próximo ciclo")
                continue

            match_start = parse_dt(detail.get("start"))
            match_end   = parse_dt(detail.get("end"))
            if not match_start:
                log.warning(f"  get_map_scoreboard({match_id}) sin 'start' válido, reintentando en el próximo ciclo")
                continue

            await db.insert_match(conn, match_id, map_name, match_start,
                                   match_end, allied_score, axis_score)

            # Lookup nombre->steam_id para convertir most_killed/death_by a IDs
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

                await db.insert_player_stats(conn, match_id, steam_id, p,
                                              most_killed_ids, death_by_ids)
                await db.upsert_player(conn, steam_id, p.get("player", ""), match_start)

            player_count = len([p for p in players if int(p.get("time_seconds") or 0) > 0])
            log.info(f"  Nueva: [{match_id}] {map_name} — {player_count} jugadores")
            new_count += 1
            await asyncio.sleep(0.3)

    return new_count


async def update_challenges_progress(pool: asyncpg.Pool,
                                      session: aiohttp.ClientSession) -> None:
    async with pool.acquire() as conn:
        await db.close_expired_custom_challenges(conn)
        await db.expire_stale_close_notifications(conn)
        challenges = await db.get_active_challenges(conn)

        for ch in challenges:
            match_ids   = None
            start_date  = ch["start_date"]
            end_date    = ch["end_date"]
            should_close = False

            if ch["period"] == "current_match":
                match_ids, should_close = await db.resolve_match_scope(conn, ch)
                if match_ids is None:
                    continue

            metrics = await db.get_challenge_metrics(conn, ch["id"])
            if not metrics:
                continue

            player_completion = {}
            player_names      = {}
            touched_players   = 0

            for metric_row in metrics:
                values = await db.fetch_metric_values(
                    conn, metric_row["metric"],
                    match_ids=match_ids, start_date=start_date, end_date=end_date,
                    param=metric_row["param"]
                )
                touched_players = max(touched_players, len(values))

                for r in values:
                    value     = float(r["value"] or 0)
                    completed = value >= float(metric_row["target"])
                    sid       = r["steam_id"]
                    player_names[sid] = r["player_name"]
                    await db.upsert_metric_progress(
                        conn, metric_row["id"], sid, r["player_name"], value, completed
                    )
                    player_completion.setdefault(sid, []).append(completed)

            for sid, flags in player_completion.items():
                all_done = all(flags) and len(flags) == len(metrics)
                await db.upsert_challenge_progress(
                    conn, ch["id"], sid, player_names.get(sid), all_done
                )

            if touched_players:
                log.info(f"  Desafío '{ch['name']}' (#{ch['id']}): {touched_players} jugadores actualizados")

            if should_close:
                await db.close_challenge(conn, ch["id"])
                log.info(f"  Desafío '{ch['name']}' (#{ch['id']}) cerrado: terminó la partida asociada")


async def run_live_progress_update(pool: asyncpg.Pool,
                                    session: aiohttp.ClientSession) -> None:
    try:
        info = await crcon.fetch_public_info(session)
    except Exception as e:
        log.warning(f"  [live] get_public_info falló: {e}")
        return

    current_map = (info or {}).get("current_map") or {}
    map_start   = current_map.get("start")
    if map_start is None:
        return

    map_start_dt = datetime.fromtimestamp(map_start, tz=timezone.utc)

    async with pool.acquire() as conn:
        eligible = await db.fetch_eligible_live_challenges(conn, map_start, map_start_dt)
        if not eligible:
            return

        try:
            live_result = await crcon.fetch_live_game_stats(session)
        except Exception as e:
            log.warning(f"  [live] get_live_game_stats falló: {e}")
            return

        live_by_player = aggregate_live_stats_by_player(live_result)
        if not live_by_player:
            return

        needs_kill_logs = await db.fetch_challenges_needing_kill_logs(conn, [ch["id"] for ch in eligible])
        live_kills_by_player = {}
        if needs_kill_logs:
            live_kills_by_player = await update_live_kills_state(session, map_start, map_start_dt)

        for ch in eligible:
            metrics = await db.get_challenge_metrics(conn, ch["id"])
            if not metrics:
                continue

            player_completion = {}
            player_names      = {}

            for metric_row in metrics:
                values = await compute_combined_metric_values(
                    conn, metric_row["metric"], live_by_player,
                    match_ids=None, start_date=ch["start_date"], end_date=ch["end_date"],
                    param=metric_row["param"], live_kills_by_player=live_kills_by_player
                )
                for r in values:
                    value     = float(r["value"] or 0)
                    completed = value >= float(metric_row["target"])
                    sid       = r["steam_id"]
                    player_names[sid] = r["player_name"]
                    await db.upsert_metric_progress(
                        conn, metric_row["id"], sid, r["player_name"], value, completed
                    )
                    player_completion.setdefault(sid, []).append(completed)

            for sid, flags in player_completion.items():
                all_done = all(flags) and len(flags) == len(metrics)
                await db.upsert_challenge_progress(
                    conn, ch["id"], sid, player_names.get(sid), all_done
                )


async def detect_and_notify_events(pool: asyncpg.Pool,
                                    session: aiohttp.ClientSession) -> None:
    try:
        info = await crcon.fetch_public_info(session)
    except Exception as e:
        log.warning(f"  [eventos] get_public_info falló: {e}")
        return

    current_map = (info or {}).get("current_map") or {}
    map_start   = current_map.get("start")
    if map_start is None:
        return

    map_start_dt = datetime.fromtimestamp(map_start, tz=timezone.utc)

    if _event_detector_state["map_start"] != map_start:
        _event_detector_state["map_start"]  = map_start
        _event_detector_state["seen_keys"]  = set()

    try:
        events = await crcon.fetch_recent_logs(session, limit=500, action="KILL")
    except Exception as e:
        log.warning(f"  [eventos] get_recent_logs falló: {e}")
        return

    seen_keys = _event_detector_state.setdefault("seen_keys", set())
    fakeos    = []

    for ev in events:
        if ev.get("action") != "KILL":
            continue
        ts_ms = ev.get("timestamp_ms")
        if ts_ms is not None and ts_ms < map_start_dt.timestamp() * 1000:
            continue

        key = make_event_key(ev)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        weapon = ev.get("weapon")
        if weapon in config.MELEE_WEAPONS:
            killer = ev.get("player_name_1") or "?"
            victim = ev.get("player_name_2") or "?"
            fakeos.append(f"🔪 **{killer}** fakeó a **{victim}** con `{weapon}`")

    if not fakeos:
        return

    async with pool.acquire() as conn:
        guilds = await db.get_guilds_with_event_channel(conn)
        for g in guilds:
            for mensaje in fakeos:
                await db.insert_event(conn, g["guild_id"], "fakeo", mensaje)

    log.info(f"  [eventos] {len(fakeos)} fakeo(s) detectado(s)")


async def backfill_match_player_stats(pool: asyncpg.Pool,
                                       session: aiohttp.ClientSession) -> None:
    """
    Repuebla match_player_stats para partidas con columnas JSONB vacías
    (guardadas antes de que se agregaran esos campos).
    Se activa con BACKFILL_MATCH_STATS=true.
    """
    log.info("=== BACKFILL de match_player_stats iniciado ===")

    async with pool.acquire() as conn:
        candidatas = await db.fetch_backfill_candidates(conn)

    total       = len(candidatas)
    actualizadas = 0
    fallidas    = []

    log.info(f"  {total} partida(s) con scoreboard incompleto, reprocesando...")

    for row in candidatas:
        match_id = row["match_id"]
        try:
            detail  = await crcon.fetch_map_scoreboard(session, int(match_id))
            players = detail.get("player_stats") or []
        except Exception as e:
            log.warning(f"  No se pudo obtener get_map_scoreboard({match_id}): {e}")
            fallidas.append(match_id)
            await asyncio.sleep(0.3)
            continue

        if not players:
            log.warning(f"  get_map_scoreboard({match_id}) devolvió vacío.")
            fallidas.append(match_id)
            await asyncio.sleep(0.3)
            continue

        name_to_id = {
            p2.get("player", ""): p2.get("player_id", "")
            for p2 in players
            if p2.get("player_id") and p2.get("player")
        }

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM match_player_stats WHERE match_id = $1", match_id)
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
                await db.insert_player_stats(conn, match_id, steam_id, p,
                                              most_killed_ids, death_by_ids)

        actualizadas += 1
        if actualizadas % 25 == 0 or actualizadas == total:
            log.info(f"  Backfill: {actualizadas}/{total} partidas reprocesadas")
        await asyncio.sleep(0.3)

    log.info(f"=== BACKFILL completado: {actualizadas} partidas actualizadas ===")
    if fallidas:
        log.warning(f"  {len(fallidas)} partida(s) pendientes: {fallidas}")