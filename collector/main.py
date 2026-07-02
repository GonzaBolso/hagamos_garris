"""
Collector — corre cada COLLECT_INTERVAL_MINUTES minutos.
Flujo:
  1. get_scoreboard_maps  → lista de partidas (IDs)
  2. get_map_scoreboard?map_id=X → stats de jugadores por partida
"""
import asyncio
import os
import logging
import json
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
BACKFILL_MATCH_STATS = os.environ.get("BACKFILL_MATCH_STATS", "").lower() in ("1", "true", "yes")

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
        # CRCON manda la mayoria de los timestamps naive en hora LOCAL del
        # servidor (UTC-3: event_time, creation_time, start/end de
        # get_scoreboard_maps). Los convertimos a UTC sumando 3 horas.
        # Excepcion: start/end de get_map_scoreboard vienen con offset
        # explicito (+00:00), asi que ese branch nunca llega aca.
        dt = dt.replace(tzinfo=timezone(timedelta(hours=-3)))
    return dt.astimezone(timezone.utc)


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



async def fetch_recent_logs(session: aiohttp.ClientSession, limit: int = 500,
                             action: str = "") -> list:
    """
    Consulta get_recent_logs (endpoint POST) — pensado para uso en vivo,
    a diferencia de get_historical_logs (que es para rangos de fecha
    puntuales, como el backfill). No tiene from/till, solo 'end' (tamaño
    de la consulta) — siempre trae "los últimos N", así que el caller es
    responsable de filtrar los que ya procesó (ver _make_event_key).
    Trae timestamp_ms (milisegundos), más preciso que el event_time de
    get_historical_logs (que es por segundo, insuficiente para
    desambiguar eventos simultáneos sin duplicarlos).
    """
    url = f"{CRCON_URL}/api/get_recent_logs"
    body = {
        "end": limit,
        "filter_action": [action] if action else [],
        "filter_player": [],
        "exact_action": bool(action),
        "inclusive_filter": True,
    }
    async with session.post(url, json=body) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"get_recent_logs falló: {data.get('error')}")
        return (data.get("result") or {}).get("logs") or []


def _make_event_key(ev: dict) -> str:
    """
    Clave única por evento, para deduplicar entre consultas sucesivas de
    fetch_recent_logs (que siempre trae 'los últimos N', con solapamiento
    natural). Combina timestamp_ms + ambos jugadores + arma — más
    confiable que comparar solo timestamps, que pueden repetirse entre
    eventos simultáneos.
    """
    return (
        f"{ev.get('timestamp_ms')}|{ev.get('player_id_1')}|"
        f"{ev.get('player_id_2')}|{ev.get('weapon')}"
    )




async def process_maps(pool: asyncpg.Pool, session: aiohttp.ClientSession, maps: list,
                        live_map_start_epoch: float = None) -> int:
    """
    live_map_start_epoch: timestamp epoch (UTC) de inicio de la partida que
    está EN CURSO ahora mismo según get_public_info. get_scoreboard_maps
    incluye esa partida en la lista aunque todavía no haya terminado, con
    un "end" que no es el cierre real (sigue corriendo mientras la partida
    sigue activa). Si la procesamos ahí, queda guardada en 'matches' con
    end_time a medio camino para siempre, porque exists=True la salta en
    los próximos ciclos. Por eso la salteamos mientras siga siendo la
    partida en vivo — recién se procesa, con datos finales, cuando ya
    terminó y aparece la próxima partida arrancada.
    """
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

            # Es la partida en curso ahora mismo? Si es así, todavía no
            # cerró de verdad — no la guardamos en este ciclo.
            if live_map_start_epoch is not None:
                m_start_raw = m.get("start")
                if m_start_raw:
                    try:
                        naive = datetime.fromisoformat(m_start_raw)
                        if naive.tzinfo is None:
                            # get_scoreboard_maps manda esto sin offset, en
                            # hora LOCAL del servidor (confirmado UTC-3).
                            naive = naive.replace(tzinfo=timezone(timedelta(hours=-3)))
                        m_start_epoch = naive.timestamp()
                        if abs(m_start_epoch - live_map_start_epoch) < 5:
                            continue  # es la partida en vivo, todavía no cerró
                    except Exception:
                        pass

            # Datos básicos del mapa
            map_info     = m.get("map") or {}
            map_name     = map_info.get("pretty_name") or map_info.get("id", "?")
            result       = m.get("result") or {}
            allied_score = result.get("allied")
            axis_score   = result.get("axis")

            # Buscar stats detallados con get_map_scoreboard. Si falla o no
            # trae "start" (puede pasar si la partida acaba de cerrar y
            # CRCON todavía no terminó de procesar su get_map_scoreboard),
            # NO insertamos la partida con datos a medias — la salteamos
            # para que el próximo ciclo la reintente. Insertarla con el
            # fallback naive de get_scoreboard_maps (sin offset, hora
            # LOCAL del servidor) reintroducía el bug de -3h en
            # start_time/end_time que ya habíamos corregido.
            try:
                detail = await fetch_map_scoreboard(session, int(match_id))
                players = detail.get("player_stats") or []
            except Exception as e:
                log.warning(f"  No se pudieron obtener stats de partida {match_id}: {e}, se reintenta en el próximo ciclo")
                continue

            match_start = parse_dt(detail.get("start"))
            match_end   = parse_dt(detail.get("end"))
            if not match_start:
                log.warning(f"  get_map_scoreboard({match_id}) sin 'start' válido, se reintenta en el próximo ciclo")
                continue

            # Insertar la partida
            await conn.execute(
                """
                INSERT INTO matches (match_id, map_name, start_time, end_time, allied_score, axis_score)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
                """,
                match_id,
                map_name,
                match_start,
                match_end,
                allied_score,
                axis_score,
            )

            # Construir lookup nombre->steam_id con todos los jugadores
            # de esta partida, para convertir most_killed/death_by a IDs.
            name_to_id = {
                p2.get("player", ""): p2.get("player_id", "")
                for p2 in players
                if p2.get("player_id") and p2.get("player")
            }

            import json as _json

            for p in players:
                steam_id = p.get("player_id", "")
                if not steam_id:
                    continue

                # Filtrar jugadores con tiempo negativo o 0 (conexiones fallidas)
                time_sec = int(p.get("time_seconds") or 0)
                if time_sec <= 0:
                    continue

                # Convertir most_killed/death_by de {nombre: count} a {steam_id: count}
                # usando el lookup del mismo match. Nombres sin steam_id conocido se descartan.
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
                    _json.dumps(p.get("kills_by_type") or {}),
                    _json.dumps(p.get("deaths_by_type") or {}),
                    _json.dumps(p.get("weapons") or {}),
                    _json.dumps(p.get("death_by_weapons") or {}),
                    _json.dumps(p.get("most_killed") or {}),
                    _json.dumps(p.get("death_by") or {}),
                    _json.dumps(most_killed_ids),
                    _json.dumps(death_by_ids),
                )

                # Mantiene actualizada la lista de jugadores conocidos
                # (usada para el autocompletado por nombre en desafíos y
                # para /hll registro). Solo actualiza el nombre si esta
                # partida es MÁS RECIENTE que la última registrada — así
                # no importa en qué orden el collector procese las
                # partidas (relevante sobre todo durante un backfill).
                await conn.execute(
                    """
                    INSERT INTO players (steam_id, player_name, last_match_start)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (steam_id) DO UPDATE
                        SET player_name = $2, last_match_start = $3
                        WHERE players.last_match_start IS NULL
                           OR $3 > players.last_match_start
                    """,
                    steam_id, p.get("player", ""), match_start,
                )

            player_count = len([p for p in players if int(p.get("time_seconds") or 0) > 0])
            log.info(f"  Nueva: [{match_id}] {map_name} — {player_count} jugadores")

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
    de la víctima) y consultan las columnas JSONB de match_player_stats.
    """
    if metric in ("kills_weapon", "kills_player", "kills_type"):
        if not param:
            return []
        # kills_weapon: suma mps.weapons->>'ARMA'
        # kills_player: suma mps.most_killed_ids->>'steam_id'
        # kills_type:   suma mps.kills_by_type->>'infantry'|'armor'|etc.
        if metric == "kills_weapon":
            jsonb_col = "weapons"
        elif metric == "kills_player":
            jsonb_col = "most_killed_ids"
        else:
            jsonb_col = "kills_by_type"
        if match_ids is not None:
            where_clause = "mps.match_id = ANY($1::varchar[])"
            params = [match_ids, param]
            param_n = 2
        else:
            where_clause = "m.start_time BETWEEN $1 AND $2"
            params = [start_date, end_date, param]
            param_n = 3
        query = f"""
            SELECT mps.steam_id, MAX(mps.player_name) AS player_name,
                   COALESCE(SUM((mps.{jsonb_col}->>${param_n})::int), 0) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            WHERE {where_clause}
              AND mps.{jsonb_col} ? ${param_n}
            GROUP BY mps.steam_id
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
    steam_id -> {kills, deaths, combat, offense, defense, support,
                 kills_by_type, player_name}
    para sumarlo fácilmente contra lo ya cerrado.
    kills_by_type incluye el desglose por tipo (infantry, armor, etc.)
    que get_live_game_stats ya provee — así kills_type funciona en vivo
    sin necesidad de parsear logs individuales.
    """
    out = {}
    for p in (live_result or {}).get("stats", []):
        steam_id = p.get("player_id")
        if not steam_id:
            continue
        out[steam_id] = {
            "player_name":   p.get("player", ""),
            "kills":         int(p.get("kills") or 0),
            "deaths":        int(p.get("deaths") or 0),
            "combat":        int(p.get("combat") or 0),
            "offense":       int(p.get("offense") or 0),
            "defense":       int(p.get("defense") or 0),
            "support":       int(p.get("support") or 0),
            "kills_by_type": p.get("kills_by_type") or {},
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

    kills_weapon/kills_player: suman el campo JSONB correspondiente en
    match_player_stats (cerrado) + el cursor en memoria de kills en vivo
    (live_kills_by_player), filtrando por arma exacta o steam_id de víctima.

    Devuelve una lista de dicts {steam_id, player_name, value} — mismo
    formato que fetch_metric_values, para que el resto del código no
    necesite distinguir entre "con live" y "sin live".
    """
    live_kills_by_player = live_kills_by_player or {}

    if metric in ("kills_weapon", "kills_player", "kills_type"):
        if not param:
            return []

        if metric == "kills_weapon":
            jsonb_col = "weapons"
        elif metric == "kills_player":
            jsonb_col = "most_killed_ids"
        else:
            jsonb_col = "kills_by_type"
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
            SELECT mps.steam_id, MAX(mps.player_name) AS player_name,
                   COALESCE(SUM((mps.{jsonb_col}->>${ param_idx })::int), 0) AS value
            FROM match_player_stats mps
            JOIN matches m USING (match_id)
            WHERE {where_clause}
              AND mps.{jsonb_col} ? ${ param_idx }
            GROUP BY mps.steam_id
            """,
            *params_closed
        )
        closed_by_player = {r["steam_id"]: (r["value"] or 0) for r in closed}
        names_by_player = {r["steam_id"]: r["player_name"] for r in closed}

        if metric == "kills_weapon":
            live_field = "by_weapon"
            live_source = live_kills_by_player   # de get_recent_logs
        elif metric == "kills_player":
            live_field = "by_victim"
            live_source = live_kills_by_player   # de get_recent_logs
        else:
            # kills_type: get_live_game_stats ya tiene kills_by_type por jugador
            # mucho mas preciso que inferirlo de los logs
            live_field = "kills_by_type"
            live_source = live_by_player          # de get_live_game_stats
        all_steam_ids = set(closed_by_player) | set(live_source)
        results = []
        for steam_id in all_steam_ids:
            live_value = live_source.get(steam_id, {}).get(live_field, {}).get(param, 0) if metric == "kills_type" else live_kills_by_player.get(steam_id, {}).get(live_field, {}).get(param, 0)
            total = closed_by_player.get(steam_id, 0) + live_value
            player_name = (
                names_by_player.get(steam_id)
                or live_source.get(steam_id, {}).get("player_name")
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
    Si cambió la partida, resetea el estado. Usa get_recent_logs (sin
    from/till — siempre trae "los últimos N"), deduplicando por clave
    compuesta (_make_event_key) en vez de solo comparar timestamps, ya
    que event_time/timestamp_ms pueden repetirse entre kills simultáneos
    y el solapamiento natural de "traer los últimos N" generaría avisos
    o conteos duplicados si solo comparáramos fecha.

    Devuelve kills_by_player: steam_id -> {
        "by_weapon": {weapon: count}, "by_victim": {victim_id: count}
    }
    """
    if _live_kills_state["map_start"] != map_start:
        _live_kills_state["map_start"] = map_start
        _live_kills_state["kills_by_player"] = {}
        _live_kills_state["seen_keys"] = set()

    try:
        events = await fetch_recent_logs(session, limit=2000, action="KILL")
    except Exception as e:
        log.warning(f"  [live] get_recent_logs falló: {e}")
        return _live_kills_state["kills_by_player"]

    seen_keys = _live_kills_state.setdefault("seen_keys", set())

    for ev in events:
        if ev.get("action") != "KILL":
            continue
        killer_id = ev.get("player_id_1")
        victim_id = ev.get("player_id_2")
        weapon = ev.get("weapon")
        if not killer_id or not victim_id:
            continue

        # Solo contamos kills de esta partida en curso (después de que
        # arrancó el mapa actual) — get_recent_logs no filtra por partida.
        ts_ms = ev.get("timestamp_ms")
        if ts_ms is not None and map_start_dt is not None:
            if ts_ms < map_start_dt.timestamp() * 1000:
                continue

        key = _make_event_key(ev)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        player_state = _live_kills_state["kills_by_player"].setdefault(
            killer_id, {"player_name": None, "by_weapon": {}, "by_victim": {}, "by_type": {}}
        )
        killer_name = ev.get("player_name_1")
        if killer_name:
            player_state["player_name"] = killer_name
        if weapon:
            player_state["by_weapon"][weapon] = player_state["by_weapon"].get(weapon, 0) + 1
        player_state["by_victim"][victim_id] = player_state["by_victim"].get(victim_id, 0) + 1
        kill_type = ev.get("kill_type") or ev.get("type_1")
        if kill_type:
            player_state["by_type"][kill_type] = player_state["by_type"].get(kill_type, 0) + 1

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
                WHERE c.id = ANY($1::int[]) AND cm.metric IN ('kills_weapon', 'kills_player', 'kills_type')
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
EVENT_DETECTOR_INTERVAL_SECONDS = 25

# Armas de cuerpo a cuerpo de HLL (una por facción: US, Alemania, URSS,
# Gran Bretaña). Un kill con cualquiera de estas se considera "fakeo" —
# evento destacado que se avisa en el canal de eventos, sin relación con
# desafíos.
MELEE_WEAPONS = {"M3 KNIFE", "FELDSPATEN", "MPL-50 SPADE", "Fairbairn–Sykes"}

# Estado del cursor de kills en vivo para la partida actual. Se resetea
# automáticamente cuando cambia map_start (otra partida). steam_id -> {
#   "by_weapon": {weapon: count}, "by_victim": {victim_id: count}
# }
_live_kills_state = {"map_start": None, "kills_by_player": {}, "seen_keys": set()}

# Cursor independiente para el detector de eventos destacados (fakeos,
# etc.) — separado de _live_kills_state porque corre siempre, sin
# importar si hay desafíos activos.
_event_detector_state = {"map_start": None, "seen_keys": set()}


async def detect_and_notify_events(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """
    Corre siempre (sin relación con desafíos activos). Si hay una partida
    en curso, revisa los KILL recientes (get_recent_logs) y, si alguno
    usó un arma de cuerpo a cuerpo (MELEE_WEAPONS) y no fue notificado
    antes (deduplicado por _make_event_key), lo encola en detected_events
    para que el bot lo notifique en el canal de eventos.
    """
    try:
        info = await fetch_public_info(session)
    except Exception as e:
        log.warning(f"  [eventos] get_public_info falló: {e}")
        return

    current_map = (info or {}).get("current_map") or {}
    map_start = current_map.get("start")
    if map_start is None:
        return  # no hay partida en curso, nada que revisar

    map_start_dt = datetime.fromtimestamp(map_start, tz=timezone.utc)

    if _event_detector_state["map_start"] != map_start:
        _event_detector_state["map_start"] = map_start
        _event_detector_state["seen_keys"] = set()

    try:
        events = await fetch_recent_logs(session, limit=500, action="KILL")
    except Exception as e:
        log.warning(f"  [eventos] get_recent_logs falló: {e}")
        return

    seen_keys = _event_detector_state.setdefault("seen_keys", set())
    fakeos = []

    for ev in events:
        if ev.get("action") != "KILL":
            continue

        ts_ms = ev.get("timestamp_ms")
        if ts_ms is not None and ts_ms < map_start_dt.timestamp() * 1000:
            continue  # kill de una partida anterior, no de la actual

        key = _make_event_key(ev)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        weapon = ev.get("weapon")
        if weapon in MELEE_WEAPONS:
            killer = ev.get("player_name_1") or "?"
            victim = ev.get("player_name_2") or "?"
            fakeos.append(f"🔪 **{killer}** fakeó a **{victim}** con `{weapon}`")

    if not fakeos:
        return

    async with pool.acquire() as conn:
        guilds = await conn.fetch(
            "SELECT guild_id FROM guild_config WHERE eventos_channel_id IS NOT NULL"
        )
        for g in guilds:
            for mensaje in fakeos:
                await conn.execute(
                    "INSERT INTO detected_events (guild_id, event_type, message) VALUES ($1, $2, $3)",
                    g["guild_id"], "fakeo", mensaje
                )

    log.info(f"  [eventos] {len(fakeos)} fakeo(s) detectado(s)")


async def main_collector_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """Loop principal: cada COLLECT_INTERVAL_MINUTES, trae partidas cerradas
    nuevas y recalcula el progreso de desafíos (incluyendo cierres por
    partida — resolve_match_scope)."""
    while True:
        try:
            # Identificamos la partida en curso (si hay una) para que
            # process_maps no la guarde a medio cerrar — ver docstring de
            # process_maps para el detalle del problema que esto evita.
            live_map_start_epoch = None
            try:
                info = await fetch_public_info(session)
                current_map = (info or {}).get("current_map") or {}
                live_map_start_epoch = current_map.get("start")
            except Exception as e:
                log.warning(f"  No se pudo obtener get_public_info para detectar partida en vivo: {e}")

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
                new = await process_maps(pool, session, maps, live_map_start_epoch=live_map_start_epoch)
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


async def event_detector_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """
    Loop separado, siempre activo (sin relación con desafíos), que
    detecta eventos destacados (fakeos con melee, y a futuro otros) y
    los encola en detected_events para que el bot los notifique.
    """
    log.info(f"Detector de eventos iniciado (cada {EVENT_DETECTOR_INTERVAL_SECONDS}s)")
    while True:
        try:
            await detect_and_notify_events(pool, session)
        except Exception as e:
            log.error(f"Error en event_detector_loop: {e}", exc_info=True)

        await asyncio.sleep(EVENT_DETECTOR_INTERVAL_SECONDS)



async def backfill_match_player_stats(pool: asyncpg.Pool, session: aiohttp.ClientSession):
    """
    Repuebla match_player_stats para todas las partidas que tengan las
    columnas JSONB vacias (kills_by_type = '{}'), lo que indica que se
    guardaron antes de que se agregaran esos campos. Re-pide
    get_map_scoreboard y reemplaza la fila entera con los datos completos.
    Se activa con BACKFILL_MATCH_STATS=true.
    """
    log.info("=== BACKFILL de match_player_stats iniciado ===")

    async with pool.acquire() as conn:
        candidatas = await conn.fetch(
            """
            SELECT DISTINCT match_id
            FROM match_player_stats
            WHERE kills_by_type = '{}'::jsonb
               OR kills_by_type IS NULL
            ORDER BY match_id::int ASC
            """
        )

    total = len(candidatas)
    log.info(f"  {total} partida(s) con scoreboard sospechoso, reprocesando...")

    actualizadas = 0
    fallidas = []

    for row in candidatas:
        match_id = row["match_id"]
        try:
            detail = await fetch_map_scoreboard(session, int(match_id))
            players = detail.get("player_stats") or []
        except Exception as e:
            log.warning(f"  No se pudo obtener get_map_scoreboard({match_id}): {e}")
            fallidas.append(match_id)
            await asyncio.sleep(0.3)
            continue

        if not players:
            log.warning(f"  get_map_scoreboard({match_id}) devolvió vacío, se deja como está.")
            fallidas.append(match_id)
            await asyncio.sleep(0.3)
            continue

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM match_player_stats WHERE match_id = $1", match_id)

            for p in players:
                steam_id = p.get("player_id", "")
                if not steam_id:
                    continue
                time_sec = int(p.get("time_seconds") or 0)
                if time_sec <= 0:
                    continue

                import json as _json
                name_to_id_b = {
                    p2.get("player", ""): p2.get("player_id", "")
                    for p2 in players
                    if p2.get("player_id") and p2.get("player")
                }
                most_killed_ids = {
                    name_to_id_b[name]: count
                    for name, count in (p.get("most_killed") or {}).items()
                    if name in name_to_id_b
                }
                death_by_ids = {
                    name_to_id_b[name]: count
                    for name, count in (p.get("death_by") or {}).items()
                    if name in name_to_id_b
                }
                await conn.execute(
                    """
                    INSERT INTO match_player_stats
                        (match_id, steam_id, player_name, kills, deaths, teamkills,
                         combat_score, offense_score, defense_score, support_score, time_seconds,
                         kills_by_type, deaths_by_type, weapons, death_by_weapons,
                         most_killed, death_by, most_killed_ids, death_by_ids)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                    ON CONFLICT (match_id, steam_id) DO UPDATE SET
                        player_name=EXCLUDED.player_name, kills=EXCLUDED.kills,
                        deaths=EXCLUDED.deaths, teamkills=EXCLUDED.teamkills,
                        combat_score=EXCLUDED.combat_score, offense_score=EXCLUDED.offense_score,
                        defense_score=EXCLUDED.defense_score, support_score=EXCLUDED.support_score,
                        time_seconds=EXCLUDED.time_seconds,
                        kills_by_type=EXCLUDED.kills_by_type,
                        deaths_by_type=EXCLUDED.deaths_by_type,
                        weapons=EXCLUDED.weapons,
                        death_by_weapons=EXCLUDED.death_by_weapons,
                        most_killed=EXCLUDED.most_killed,
                        death_by=EXCLUDED.death_by,
                        most_killed_ids=EXCLUDED.most_killed_ids,
                        death_by_ids=EXCLUDED.death_by_ids
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
                    _json.dumps(p.get("kills_by_type") or {}),
                    _json.dumps(p.get("deaths_by_type") or {}),
                    _json.dumps(p.get("weapons") or {}),
                    _json.dumps(p.get("death_by_weapons") or {}),
                    _json.dumps(p.get("most_killed") or {}),
                    _json.dumps(p.get("death_by") or {}),
                    _json.dumps(most_killed_ids),
                    _json.dumps(death_by_ids),
                )

        actualizadas += 1
        if actualizadas % 25 == 0 or actualizadas == total:
            log.info(f"  Backfill match_player_stats: {actualizadas}/{total} partidas reprocesadas")

        await asyncio.sleep(0.3)  # no saturar CRCON

    log.info(
        f"=== BACKFILL de match_player_stats completado: {actualizadas} partidas actualizadas ==="
    )
    if fallidas:
        log.warning(
            f"  {len(fallidas)} partida(s) no se pudieron actualizar: {fallidas}"
        )


async def run():
    log.info("Collector iniciado")
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        if BACKFILL_MATCH_STATS:
            await backfill_match_player_stats(pool, session)
            log.info("Backfill terminado, el proceso va a salir ahora.")
            return

        await asyncio.gather(
            main_collector_loop(pool, session),
            live_polling_loop(pool, session),
            event_detector_loop(pool, session),
        )


if __name__ == "__main__":
    asyncio.run(run())