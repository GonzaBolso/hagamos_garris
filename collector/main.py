"""
Collector — corre cada COLLECT_INTERVAL_MINUTES minutos.
Flujo:
  1. get_scoreboard_maps  → lista de partidas (IDs)
  2. get_map_scoreboard?map_id=X → stats de jugadores por partida
"""
import asyncio
import os
import logging
from datetime import datetime, timezone, timedelta

import aiohttp
import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [collector] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

CRCON_URL     = os.environ["CRCON_URL"].rstrip("/")   # ej: http://IP:7010
CRCON_API_KEY = os.environ.get("CRCON_API_KEY", "")   # opcional en el 7010
INTERVAL      = int(os.environ.get("COLLECT_INTERVAL_MINUTES", 30)) * 60

DB_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

# Headers opcionales (7010 puede no necesitar auth)
HEADERS = {"Content-Type": "application/json"}
if CRCON_API_KEY:
    HEADERS["Authorization"] = f"Bearer {CRCON_API_KEY}"


def parse_dt(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # CRCON manda timestamps naive; asumimos UTC (consistente con los
        # epoch de start/end de get_scoreboard_maps, que sí son UTC).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def fetch_scoreboard_maps(session: aiohttp.ClientSession, page: int = 1) -> dict:
    url = f"{CRCON_URL}/api/get_scoreboard_maps"
    async with session.get(url, params={"page": page, "page_size": 100}) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"get_scoreboard_maps falló: {data.get('error')}")
        return data.get("result", {})


async def fetch_map_scoreboard(session: aiohttp.ClientSession, map_id: int) -> dict:
    url = f"{CRCON_URL}/api/get_map_scoreboard"
    async with session.get(url, params={"map_id": map_id}) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"get_map_scoreboard({map_id}) falló: {data.get('error')}")
        return data.get("result", {})


async def fetch_public_info(session: aiohttp.ClientSession) -> dict:
    url = f"{CRCON_URL}/api/get_public_info"
    async with session.get(url) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"get_public_info falló: {data.get('error')}")
        return data.get("result", {})


async def fetch_live_game_stats(session: aiohttp.ClientSession) -> dict:
    url = f"{CRCON_URL}/api/get_live_game_stats"
    async with session.get(url) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"get_live_game_stats falló: {data.get('error')}")
        return data.get("result", {})


async def fetch_historical_logs(session: aiohttp.ClientSession, from_: str = None,
                                 till: str = None, limit: int = 500) -> list:
    """
    Consulta get_historical_logs (endpoint POST). Devuelve la lista de
    eventos (result) sin filtrar — el caller decide qué 'type' usar
    (ej. solo 'KILL', sin 'TEAM KILL').
    from_/till: strings ISO datetime, o None para no acotar ese extremo.
    """
    url = f"{CRCON_URL}/api/get_historical_logs"
    body = {
        "player_name": "",
        "action": "",
        "player_id": "",
        "from": from_,
        "till": till,
        "limit": limit,
        "time_sort": "desc",
        "exact_player": False,
        "exact_action": False,
        "server_filter": "",
    }
    async with session.post(url, json=body) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"get_historical_logs falló: {data.get('error')}")
        return data.get("result") or []


async def save_kill_events_for_match(conn, session: aiohttp.ClientSession,
                                      match_id: str, start_iso: str, end_iso: str):
    """
    Al cerrar una partida, consulta get_historical_logs acotado al rango
    de esa partida (start/end), y guarda cada KILL (no TEAM KILL) en
    kill_events, asociado a match_id. Se usa para desafíos tipo
    'kills_weapon' (matar con un arma específica) y 'kills_player'
    (matar repetidamente a un jugador puntual).
    """
    if not start_iso:
        return  # sin start no podemos acotar el rango, mejor no traer nada

    try:
        events = await fetch_historical_logs(session, from_=start_iso, till=end_iso, limit=1000)
    except Exception as e:
        log.warning(f"  No se pudo obtener historical_logs para partida {match_id}: {e}")
        return

    saved = 0
    for ev in events:
        if ev.get("type") != "KILL":
            continue  # excluye TEAM KILL y todo lo demás

        killer_id = ev.get("player1_id")
        victim_id = ev.get("player2_id")
        if not killer_id or not victim_id:
            continue

        await conn.execute(
            """
            INSERT INTO kill_events
                (match_id, event_time, killer_id, killer_name, victim_id, victim_name, weapon)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            match_id,
            parse_dt(ev.get("event_time")),
            killer_id,
            ev.get("player1_name", ""),
            victim_id,
            ev.get("player2_name", ""),
            ev.get("weapon"),
        )
        saved += 1

    if saved:
        log.info(f"  [{match_id}] {saved} kill_events guardados")


async def process_maps(pool: asyncpg.Pool, session: aiohttp.ClientSession, maps: list) -> int:
    new_count = 0

    async with pool.acquire() as conn:
        for m in maps:
            match_id = str(m.get("id", ""))
            if not match_id:
                continue

            # Ya procesada?
            exists = await conn.fetchval(
                "SELECT 1 FROM matches WHERE match_id = $1", match_id
            )
            if exists:
                continue

            # Datos básicos del mapa
            map_info     = m.get("map") or {}
            map_name     = map_info.get("pretty_name") or map_info.get("id", "?")
            result       = m.get("result") or {}
            allied_score = result.get("allied")
            axis_score   = result.get("axis")

            # Insertar la partida
            await conn.execute(
                """
                INSERT INTO matches (match_id, map_name, start_time, end_time, allied_score, axis_score)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
                """,
                match_id,
                map_name,
                parse_dt(m.get("start")),
                parse_dt(m.get("end")),
                allied_score,
                axis_score,
            )

            # Buscar stats detallados con get_map_scoreboard
            try:
                detail = await fetch_map_scoreboard(session, int(match_id))
                players = detail.get("player_stats") or []
            except Exception as e:
                log.warning(f"  No se pudieron obtener stats de partida {match_id}: {e}")
                players = []

            for p in players:
                steam_id = p.get("player_id", "")
                if not steam_id:
                    continue

                # Filtrar jugadores con tiempo negativo o 0 (conexiones fallidas)
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

                # Mantiene actualizada la lista de jugadores conocidos
                # (usada para el autocompletado por nombre en desafíos).
                await conn.execute(
                    """
                    INSERT INTO players (steam_id, player_name)
                    VALUES ($1, $2)
                    ON CONFLICT (steam_id) DO UPDATE SET player_name = $2
                    """,
                    steam_id, p.get("player", ""),
                )

            player_count = len([p for p in players if int(p.get("time_seconds") or 0) > 0])
            log.info(f"  Nueva: [{match_id}] {map_name} — {player_count} jugadores")

            await save_kill_events_for_match(
                conn, session, match_id, m.get("start"), m.get("end")
            )

            new_count += 1

            # Pequeña pausa para no martillar la API
            await asyncio.sleep(0.3)

    return new_count


METRIC_COLUMN = {
    "kills":    "kills",
    "deaths":   "deaths",
    "matches":  None,        # se cuenta como COUNT(*) de partidas en el rango
    "combat":   "combat_score",
    "offense":  "offense_score",
    "defense":  "defense_score",
    "support":  "support_score",
    "kd_ratio": None,        # se calcula aparte: SUM(kills)/SUM(deaths)
}


async def fetch_metric_values(conn, metric: str, match_ids: list = None,
                               start_date=None, end_date=None, param: str = None) -> list:
    """
    Devuelve [{steam_id, player_name, value}, ...] para una métrica dada,
    filtrando por una lista explícita de match_id (partidas puntuales)
    o por rango de fechas [start_date, end_date].
    kills_weapon/kills_player necesitan 'param' (arma exacta, o steam_id
    de la víctima) y consultan kill_events en vez de match_player_stats.
    """
    if metric in ("kills_weapon", "kills_player"):
        if not param:
            return []
        filter_col = "weapon" if metric == "kills_weapon" else "victim_id"
        if match_ids is not None:
            where_clause = f"ke.match_id = ANY($1::varchar[]) AND ke.{filter_col} = $2"
            params = [match_ids, param]
        else:
            where_clause = f"m.start_time BETWEEN $1 AND $2 AND ke.{filter_col} = $3"
            params = [start_date, end_date, param]
        query = f"""
            SELECT ke.killer_id AS steam_id, MAX(ke.killer_name) AS player_name,
                   COUNT(*) AS value
            FROM kill_events ke
            JOIN matches m USING (match_id)
            WHERE {where_clause}
            GROUP BY ke.killer_id
        """
        return await conn.fetch(query, *params)

    col = METRIC_COLUMN.get(metric)

    if match_ids is not None:
        where_clause = "mps.match_id = ANY($1::varchar[])"
        params = [match_ids]
    else:
        where_clause = "m.start_time BETWEEN $1 AND $2"
        params = [start_date, end_date]

    if metric == "matches":
        query = f"""
            SELECT mps.steam_id, MAX(mps.player_name) AS player_name,
                   COUNT(DISTINCT mps.match_id) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            WHERE {where_clause}
            GROUP BY mps.steam_id
        """
    elif metric == "kd_ratio":
        query = f"""
            SELECT mps.steam_id, MAX(mps.player_name) AS player_name,
                   CASE WHEN SUM(mps.deaths) = 0 THEN SUM(mps.kills)::FLOAT
                        ELSE ROUND((SUM(mps.kills)::NUMERIC / SUM(mps.deaths)), 2)
                   END AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            WHERE {where_clause}
            GROUP BY mps.steam_id
        """
    else:
        query = f"""
            SELECT mps.steam_id, MAX(mps.player_name) AS player_name,
                   SUM(mps.{col}) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            WHERE {where_clause}
            GROUP BY mps.steam_id
        """

    return await conn.fetch(query, *params)


# Mapeo: métrica de desafío -> campo correspondiente en get_live_game_stats.
# kd_ratio no está acá porque se combina aparte, a partir de kills+deaths.
LIVE_METRIC_FIELD = {
    "kills":   "kills",
    "combat":  "combat",
    "offense": "offense",
    "defense": "defense",
    "support": "support",
}


def aggregate_live_stats_by_player(live_result: dict) -> dict:
    """
    Convierte la respuesta de get_live_game_stats en un dict
    steam_id -> {kills, deaths, combat, offense, defense, support, player_name}
    para sumarlo fácilmente contra lo ya cerrado.
    """
    out = {}
    for p in (live_result or {}).get("stats", []):
        steam_id = p.get("player_id")
        if not steam_id:
            continue
        out[steam_id] = {
            "player_name": p.get("player", ""),
            "kills":   int(p.get("kills") or 0),
            "deaths":  int(p.get("deaths") or 0),
            "combat":  int(p.get("combat") or 0),
            "offense": int(p.get("offense") or 0),
            "defense": int(p.get("defense") or 0),
            "support": int(p.get("support") or 0),
        }
    return out


async def compute_combined_metric_values(conn, metric: str, live_by_player: dict,
                                          match_ids: list = None,
                                          start_date=None, end_date=None,
                                          param: str = None,
                                          live_kills_by_player: dict = None) -> list:
    """
    Igual que fetch_metric_values, pero sumándole el aporte de la partida
    en vivo (live_by_player, ya armado por aggregate_live_stats_by_player)
    a cada jugador. Para kd_ratio, combina kills+deaths de ambas fuentes
    antes de calcular el ratio (más preciso que combinar dos ratios).

    kills_weapon/kills_player: cuentan filas de kill_events (cerrado) +
    el cursor en memoria de kills en vivo (live_kills_by_player), filtrando
    por arma exacta (param) o por steam_id de víctima (param).

    Devuelve una lista de dicts {steam_id, player_name, value} — mismo
    formato que fetch_metric_values, para que el resto del código no
    necesite distinguir entre "con live" y "sin live".
    """
    live_kills_by_player = live_kills_by_player or {}

    if metric in ("kills_weapon", "kills_player"):
        if not param:
            return []  # sin arma/jugador especificado no hay nada que contar

        filter_col = "weapon" if metric == "kills_weapon" else "victim_id"

        if match_ids is not None:
            where_clause = f"ke.match_id = ANY($1::varchar[]) AND ke.{filter_col} = $2"
            params = [match_ids, param]
        else:
            where_clause = f"m.start_time BETWEEN $1 AND $2 AND ke.{filter_col} = $3"
            params = [start_date, end_date, param]

        closed = await conn.fetch(
            f"""
            SELECT ke.killer_id AS steam_id, MAX(ke.killer_name) AS player_name,
                   COUNT(*) AS value
            FROM kill_events ke
            JOIN matches m USING (match_id)
            WHERE {where_clause}
            GROUP BY ke.killer_id
            """,
            *params
        )
        closed_by_player = {r["steam_id"]: (r["value"] or 0) for r in closed}
        names_by_player = {r["steam_id"]: r["player_name"] for r in closed}

        live_field = "by_weapon" if metric == "kills_weapon" else "by_victim"
        all_steam_ids = set(closed_by_player) | set(live_kills_by_player)
        results = []
        for steam_id in all_steam_ids:
            live_value = live_kills_by_player.get(steam_id, {}).get(live_field, {}).get(param, 0)
            total = closed_by_player.get(steam_id, 0) + live_value
            player_name = (
                names_by_player.get(steam_id)
                or live_kills_by_player.get(steam_id, {}).get("player_name")
                or steam_id
            )
            results.append({"steam_id": steam_id, "player_name": player_name, "value": total})
        return results

    if metric == "kd_ratio":
        # Necesitamos kills y deaths de lo cerrado por separado para
        # combinarlos correctamente con el live.
        closed_kills = await fetch_metric_values(conn, "kills", match_ids, start_date, end_date)
        # "deaths" no es una métrica de desafío válida, pero fetch_metric_values
        # soporta cualquier columna de METRIC_COLUMN con el camino genérico;
        # la consultamos igual armando la query a mano para no tocar esa función.
        col = "deaths"
        if match_ids is not None:
            where_clause = "mps.match_id = ANY($1::varchar[])"
            params = [match_ids]
        else:
            where_clause = "m.start_time BETWEEN $1 AND $2"
            params = [start_date, end_date]
        closed_deaths = await conn.fetch(
            f"""
            SELECT mps.steam_id, MAX(mps.player_name) AS player_name,
                   SUM(mps.{col}) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            WHERE {where_clause}
            GROUP BY mps.steam_id
            """,
            *params
        )

        kills_by_player = {r["steam_id"]: (r["value"] or 0) for r in closed_kills}
        deaths_by_player = {r["steam_id"]: (r["value"] or 0) for r in closed_deaths}
        names_by_player = {r["steam_id"]: r["player_name"] for r in closed_kills}
        names_by_player.update({r["steam_id"]: r["player_name"] for r in closed_deaths})

        all_steam_ids = set(kills_by_player) | set(deaths_by_player) | set(live_by_player)
        results = []
        for steam_id in all_steam_ids:
            total_kills = kills_by_player.get(steam_id, 0) + live_by_player.get(steam_id, {}).get("kills", 0)
            total_deaths = deaths_by_player.get(steam_id, 0) + live_by_player.get(steam_id, {}).get("deaths", 0)
            ratio = float(total_kills) if total_deaths == 0 else round(total_kills / total_deaths, 2)
            player_name = names_by_player.get(steam_id) or live_by_player.get(steam_id, {}).get("player_name")
            results.append({"steam_id": steam_id, "player_name": player_name, "value": ratio})
        return results

    if metric == "matches":
        # Una sola partida en vivo cuenta como 1 si el jugador tiene algo
        # registrado en el live; se suma a las partidas ya cerradas.
        closed = await fetch_metric_values(conn, "matches", match_ids, start_date, end_date)
        closed_by_player = {r["steam_id"]: (r["value"] or 0) for r in closed}
        names_by_player = {r["steam_id"]: r["player_name"] for r in closed}

        all_steam_ids = set(closed_by_player) | set(live_by_player)
        results = []
        for steam_id in all_steam_ids:
            total = closed_by_player.get(steam_id, 0) + (1 if steam_id in live_by_player else 0)
            player_name = names_by_player.get(steam_id) or live_by_player.get(steam_id, {}).get("player_name")
            results.append({"steam_id": steam_id, "player_name": player_name, "value": total})
        return results

    live_field = LIVE_METRIC_FIELD.get(metric)
    closed = await fetch_metric_values(conn, metric, match_ids, start_date, end_date)
    closed_by_player = {r["steam_id"]: (r["value"] or 0) for r in closed}
    names_by_player = {r["steam_id"]: r["player_name"] for r in closed}

    all_steam_ids = set(closed_by_player) | set(live_by_player)
    results = []
    for steam_id in all_steam_ids:
        live_value = live_by_player.get(steam_id, {}).get(live_field, 0) if live_field else 0
        total = closed_by_player.get(steam_id, 0) + live_value
        player_name = names_by_player.get(steam_id) or live_by_player.get(steam_id, {}).get("player_name")
        results.append({"steam_id": steam_id, "player_name": player_name, "value": total})
    return results


async def resolve_match_scope(conn, challenge) -> tuple:
    """
    Para desafíos 'current_match' YA ACTIVADOS (map_start seteado), busca
    si la partida que están siguiendo ya quedó cerrada en 'matches' (por
    start_time, con tolerancia de unos segundos respecto al timestamp de
    get_public_info). Devuelve (match_ids, should_close).
    should_close=True si la partida ya cerró y el desafío debe desactivarse.
    """
    if challenge["map_start"] is None:
        return None, False  # map_start ausente: desafío viejo creado antes de esta lógica

    if challenge["match_id"] is not None:
        # Ya estaba resuelto en un ciclo anterior; sigue cerrado, nada que hacer.
        return [challenge["match_id"]], False

    map_start_dt = datetime.fromtimestamp(challenge["map_start"], tz=timezone.utc)
    closed = await conn.fetchrow(
        """
        SELECT match_id FROM matches
        WHERE start_time BETWEEN $1 AND $2
        ORDER BY start_time ASC
        LIMIT 1
        """,
        map_start_dt - timedelta(seconds=30),
        map_start_dt + timedelta(seconds=30),
    )

    if not closed:
        return None, False  # la partida sigue en curso, todavía no cerró

    await conn.execute(
        "UPDATE challenges SET match_id = $1 WHERE id = $2",
        closed["match_id"], challenge["id"]
    )
    return [closed["match_id"]], True


async def update_live_kills_state(session: aiohttp.ClientSession, map_start: int, map_start_dt) -> dict:
    """
    Mantiene _live_kills_state al día para la partida en curso (map_start).
    Si cambió la partida, resetea el estado. Consulta solo los KILL nuevos
    desde el último evento ya contado (cursor por event_time), evitando
    contar el mismo kill dos veces entre ciclos sucesivos.

    Devuelve kills_by_player: steam_id -> {
        "by_weapon": {weapon: count}, "by_victim": {victim_id: count}
    }
    """
    if _live_kills_state["map_start"] != map_start:
        _live_kills_state["map_start"] = map_start
        _live_kills_state["last_event_time"] = map_start_dt
        _live_kills_state["kills_by_player"] = {}

    from_iso = _live_kills_state["last_event_time"].isoformat() if _live_kills_state["last_event_time"] else None

    try:
        events = await fetch_historical_logs(session, from_=from_iso, limit=500)
    except Exception as e:
        log.warning(f"  [live] get_historical_logs falló: {e}")
        return _live_kills_state["kills_by_player"]

    newest_time = _live_kills_state["last_event_time"]
    for ev in events:
        if ev.get("type") != "KILL":
            continue
        killer_id = ev.get("player1_id")
        victim_id = ev.get("player2_id")
        weapon = ev.get("weapon")
        if not killer_id or not victim_id:
            continue

        ev_time = parse_dt(ev.get("event_time"))
        if ev_time and (newest_time is None or ev_time > newest_time):
            newest_time = ev_time

        player_state = _live_kills_state["kills_by_player"].setdefault(
            killer_id, {"player_name": None, "by_weapon": {}, "by_victim": {}}
        )
        killer_name = ev.get("player1_name")
        if killer_name:
            player_state["player_name"] = killer_name
        if weapon:
            player_state["by_weapon"][weapon] = player_state["by_weapon"].get(weapon, 0) + 1
        player_state["by_victim"][victim_id] = player_state["by_victim"].get(victim_id, 0) + 1

    if newest_time:
        _live_kills_state["last_event_time"] = newest_time

    return _live_kills_state["kills_by_player"]


async def run_live_progress_update(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """
    Corre cada ~20-30 seg (loop separado del ciclo principal de 10-30 min).
    Calcula el progreso "en vivo" de la partida en curso y lo suma al
    progreso ya guardado de partidas cerradas, para:
      - Desafíos 'current_match' activos sin match_id resuelto todavía
        (su partida sigue en curso).
      - Desafíos 'custom' activos cuyo start_date sea anterior o igual
        al inicio de la partida en curso (aunque el end_date ya haya
        pasado — alcanza con que la partida haya arrancado a tiempo).

    Si no hay ninguna partida en curso identificable, o no hay desafíos
    elegibles, no hace nada (early return).
    """
    try:
        info = await fetch_public_info(session)
    except Exception as e:
        log.warning(f"  [live] get_public_info falló: {e}")
        return

    current_map = (info or {}).get("current_map") or {}
    map_start = current_map.get("start")
    if map_start is None:
        return

    map_start_dt = datetime.fromtimestamp(map_start, tz=timezone.utc)

    async with pool.acquire() as conn:
        eligible = await conn.fetch(
            """
            SELECT * FROM challenges
            WHERE active = TRUE
              AND (
                    (period = 'current_match' AND match_id IS NULL AND map_start = $1)
                 OR (period = 'custom' AND start_date <= $2)
              )
            """,
            map_start, map_start_dt
        )

        if not eligible:
            return

        try:
            live_result = await fetch_live_game_stats(session)
        except Exception as e:
            log.warning(f"  [live] get_live_game_stats falló: {e}")
            return

        live_by_player = aggregate_live_stats_by_player(live_result)
        if not live_by_player:
            return  # partida sin jugadores con datos todavía (recién arrancó)

        # Solo consultamos los logs de kills en vivo si hace falta (algún
        # desafío elegible usa kills_weapon/kills_player) — evita pegarle
        # a get_historical_logs cuando no hace falta.
        needs_kill_logs = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM challenge_metrics cm
                JOIN challenges c ON c.id = cm.challenge_id
                WHERE c.id = ANY($1::int[]) AND cm.metric IN ('kills_weapon', 'kills_player')
            )
            """,
            [ch["id"] for ch in eligible]
        )
        live_kills_by_player = {}
        if needs_kill_logs:
            live_kills_by_player = await update_live_kills_state(session, map_start, map_start_dt)

        for ch in eligible:
            metrics = await conn.fetch(
                "SELECT id, metric, target, param FROM challenge_metrics WHERE challenge_id = $1",
                ch["id"]
            )
            if not metrics:
                continue

            player_completion = {}
            player_names = {}

            for metric_row in metrics:
                values = await compute_combined_metric_values(
                    conn, metric_row["metric"], live_by_player,
                    match_ids=None, start_date=ch["start_date"], end_date=ch["end_date"],
                    param=metric_row["param"], live_kills_by_player=live_kills_by_player
                )
                for r in values:
                    value = float(r["value"] or 0)
                    completed = value >= float(metric_row["target"])
                    steam_id = r["steam_id"]
                    player_names[steam_id] = r["player_name"]

                    await conn.execute(
                        """
                        INSERT INTO challenge_metric_progress
                            (challenge_metric_id, steam_id, player_name, progress, completed)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (challenge_metric_id, steam_id) DO UPDATE
                            SET progress = $4, player_name = $3, completed = $5, updated_at = NOW()
                        """,
                        metric_row["id"], steam_id, r["player_name"], value, completed
                    )
                    player_completion.setdefault(steam_id, []).append(completed)

            for steam_id, flags in player_completion.items():
                all_completed = all(flags) and len(flags) == len(metrics)
                await conn.execute(
                    """
                    INSERT INTO challenge_progress
                        (challenge_id, steam_id, player_name, completed, completed_at)
                    VALUES ($1, $2, $3, $4, CASE WHEN $4 THEN NOW() ELSE NULL END)
                    ON CONFLICT (challenge_id, steam_id) DO UPDATE
                        SET player_name = $3,
                            completed = $4,
                            completed_at = CASE
                                WHEN $4 AND challenge_progress.completed = FALSE
                                THEN NOW()
                                ELSE challenge_progress.completed_at
                            END,
                            updated_at = NOW()
                    """,
                    ch["id"], steam_id, player_names.get(steam_id), all_completed
                )


async def expire_stale_close_notifications(conn):
    """
    Si un desafío se cerró pero el guild nunca configuró challenge_channel_id
    (o el bot no pudo mandar el mensaje por algún motivo), la notificación
    quedaría pendiente para siempre. Después de 30 minutos sin poder
    enviarse, se descarta (se apaga la marca, sin mandar nada).
    """
    result = await conn.execute(
        """
        UPDATE challenges
        SET pending_close_notification = FALSE
        WHERE pending_close_notification = TRUE
          AND closed_at IS NOT NULL
          AND closed_at <= NOW() - INTERVAL '30 minutes'
        """
    )
    if result != "UPDATE 0":
        log.info(f"  Notificaciones de cierre descartadas por vencimiento (30 min sin canal configurado): {result}")


async def close_expired_custom_challenges(conn):
    """
    Los desafíos 'custom' tienen una fecha_fin fija (end_date). Hasta ahora,
    al vencer simplemente dejaban de actualizarse (el query principal los
    excluye con end_date > NOW()), pero quedaban con active=TRUE para
    siempre, sin cerrarse de verdad. Esto los cierra y marca la
    notificación pendiente, igual que se hace con current_match.
    """
    expired = await conn.fetch(
        """
        SELECT id, name FROM challenges
        WHERE active = TRUE AND period = 'custom' AND end_date <= NOW()
        """
    )
    for ch in expired:
        await conn.execute(
            "UPDATE challenges SET active = FALSE, pending_close_notification = TRUE, closed_at = NOW() WHERE id = $1",
            ch["id"]
        )
        log.info(f"  Desafío '{ch['name']}' (#{ch['id']}) cerrado: venció su fecha_fin")


async def update_challenges_progress(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """
    Recalcula el progreso de cada jugador para todos los desafíos activos.
    Soporta múltiples métricas por desafío (AND: todas deben cumplirse)
    y períodos por fecha (custom) o por partida (current_match).
    """
    async with pool.acquire() as conn:
        await close_expired_custom_challenges(conn)
        await expire_stale_close_notifications(conn)

        challenges = await conn.fetch(
            """
            SELECT * FROM challenges
            WHERE active = TRUE
              AND (end_date IS NULL OR end_date > NOW())
            """
        )

        for ch in challenges:
            match_ids   = None
            start_date  = ch["start_date"]
            end_date    = ch["end_date"]
            should_close = False

            if ch["period"] == "current_match":
                match_ids, should_close = await resolve_match_scope(conn, ch)
                if match_ids is None:
                    continue  # todavía pendiente o la partida sigue en curso

            metrics = await conn.fetch(
                "SELECT id, metric, target, param FROM challenge_metrics WHERE challenge_id = $1",
                ch["id"]
            )
            if not metrics:
                continue

            # steam_id -> {metric: completed_bool}
            player_completion = {}
            player_names = {}
            touched_players = 0

            for metric_row in metrics:
                values = await fetch_metric_values(
                    conn, metric_row["metric"],
                    match_ids=match_ids, start_date=start_date, end_date=end_date,
                    param=metric_row["param"]
                )
                touched_players = max(touched_players, len(values))

                for r in values:
                    value     = float(r["value"] or 0)
                    completed = value >= float(metric_row["target"])
                    steam_id  = r["steam_id"]
                    player_names[steam_id] = r["player_name"]

                    await conn.execute(
                        """
                        INSERT INTO challenge_metric_progress
                            (challenge_metric_id, steam_id, player_name, progress, completed)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (challenge_metric_id, steam_id) DO UPDATE
                            SET progress = $4, player_name = $3, completed = $5, updated_at = NOW()
                        """,
                        metric_row["id"], steam_id, r["player_name"], value, completed
                    )

                    player_completion.setdefault(steam_id, []).append(completed)

            # Consolidar: completed = TRUE solo si TODAS las métricas dieron TRUE
            for steam_id, flags in player_completion.items():
                all_completed = all(flags) and len(flags) == len(metrics)

                await conn.execute(
                    """
                    INSERT INTO challenge_progress
                        (challenge_id, steam_id, player_name, completed, completed_at)
                    VALUES ($1, $2, $3, $4, CASE WHEN $4 THEN NOW() ELSE NULL END)
                    ON CONFLICT (challenge_id, steam_id) DO UPDATE
                        SET player_name = $3,
                            completed = $4,
                            completed_at = CASE
                                WHEN $4 AND challenge_progress.completed = FALSE
                                THEN NOW()
                                ELSE challenge_progress.completed_at
                            END,
                            updated_at = NOW()
                    """,
                    ch["id"], steam_id, player_names.get(steam_id), all_completed
                )

            if touched_players:
                log.info(f"  Desafío '{ch['name']}' (#{ch['id']}): {touched_players} jugadores actualizados")

            if should_close:
                await conn.execute(
                    "UPDATE challenges SET active = FALSE, pending_close_notification = TRUE, closed_at = NOW() WHERE id = $1",
                    ch["id"]
                )
                log.info(f"  Desafío '{ch['name']}' (#{ch['id']}) cerrado: terminó la partida asociada")


LIVE_POLL_INTERVAL_SECONDS = 25

# Estado del cursor de kills en vivo para la partida actual. Se resetea
# automáticamente cuando cambia map_start (otra partida). steam_id -> {
#   "by_weapon": {weapon: count}, "by_victim": {victim_id: count}
# }
_live_kills_state = {"map_start": None, "last_event_time": None, "kills_by_player": {}}


async def main_collector_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """Loop principal: cada COLLECT_INTERVAL_MINUTES, trae partidas cerradas
    nuevas y recalcula el progreso de desafíos (incluyendo cierres por
    partida — resolve_match_scope)."""
    while True:
        try:
            total_new = 0
            page = 1

            while True:
                log.info(f"Consultando get_scoreboard_maps página {page}...")
                result    = await fetch_scoreboard_maps(session, page=page)
                maps      = result.get("maps", [])
                total     = result.get("total", 0)
                page_size = result.get("page_size", 100)

                if not maps:
                    break

                log.info(f"  Página {page}: {len(maps)} partidas (total CRCON: {total})")
                new = await process_maps(pool, session, maps)
                total_new += new

                # Si ninguna partida de esta página era nueva, las siguientes
                # tampoco lo serán (ordenadas de más nueva a más vieja)
                if new == 0:
                    log.info("  No hay más partidas nuevas, deteniendo paginación")
                    break

                # Si ya procesamos todas las páginas
                if page * page_size >= total:
                    break

                page += 1

            log.info(f"Total partidas nuevas guardadas: {total_new}")

            log.info("Actualizando progreso de desafíos...")
            await update_challenges_progress(pool, session)

        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)

        log.info(f"Próxima ejecución en {INTERVAL // 60} minutos")
        await asyncio.sleep(INTERVAL)


async def live_polling_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """
    Loop separado, mucho más frecuente (cada LIVE_POLL_INTERVAL_SECONDS),
    que actualiza el progreso "en vivo" de desafíos current_match/custom
    elegibles mientras la partida sigue en curso. Ver run_live_progress_update.
    """
    log.info(f"Live polling iniciado (cada {LIVE_POLL_INTERVAL_SECONDS}s)")
    while True:
        try:
            await run_live_progress_update(pool, session)
        except Exception as e:
            log.error(f"Error en live_polling_loop: {e}", exc_info=True)

        await asyncio.sleep(LIVE_POLL_INTERVAL_SECONDS)


async def run():
    log.info("Collector iniciado")
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        await asyncio.gather(
            main_collector_loop(pool, session),
            live_polling_loop(pool, session),
        )


if __name__ == "__main__":
    asyncio.run(run())