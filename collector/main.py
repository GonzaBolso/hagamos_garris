"""
Collector — corre cada COLLECT_INTERVAL_MINUTES minutos.
Flujo:
  1. get_scoreboard_maps  → lista de partidas (IDs)
  2. get_map_scoreboard?map_id=X → stats de jugadores por partida
"""
import asyncio
import os
import logging
from datetime import datetime

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
    return datetime.fromisoformat(s)


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
                               start_date=None, end_date=None) -> list:
    """
    Devuelve [{steam_id, player_name, value}, ...] para una métrica dada,
    filtrando por una lista explícita de match_id (partidas puntuales)
    o por rango de fechas [start_date, end_date].
    """
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


async def resolve_match_scope(conn, challenge) -> tuple:
    """
    Para desafíos por partida (current_match / next_match), determina
    qué match_id(s) corresponden. Devuelve (match_ids, should_close).
    should_close=True si el desafío ya terminó y debe desactivarse.
    """
    period = challenge["period"]

    if period == "current_match":
        # La "partida actual" es la más reciente que tengamos guardada.
        latest = await conn.fetchrow(
            "SELECT match_id, end_time FROM matches ORDER BY start_time DESC LIMIT 1"
        )
        if not latest:
            return None, False

        if challenge["match_id"] is None:
            # Primera vez que vemos este desafío: lo asociamos a la partida en curso.
            await conn.execute(
                "UPDATE challenges SET match_id = $1, start_date = NOW() WHERE id = $2",
                latest["match_id"], challenge["id"]
            )
            return [latest["match_id"]], False

        if challenge["match_id"] != latest["match_id"]:
            # Cambió el mapa: la partida asociada ya terminó → cerramos el desafío.
            return [challenge["match_id"]], True

        return [challenge["match_id"]], False

    elif period == "next_match":
        latest = await conn.fetchrow(
            "SELECT match_id FROM matches ORDER BY start_time DESC LIMIT 1"
        )
        if not latest:
            return None, False

        if challenge["match_id"] is None:
            # Todavía no arrancó: lo asociamos a la PRÓXIMA partida que aparezca
            # distinta a la que existía al crear el desafío. Para simplificar,
            # lo asociamos directamente a la última partida vista a partir de ahora.
            await conn.execute(
                "UPDATE challenges SET match_id = $1, start_date = NOW() WHERE id = $2",
                latest["match_id"], challenge["id"]
            )
            return [latest["match_id"]], False

        if challenge["match_id"] != latest["match_id"]:
            # La partida asociada ya pasó y cambiamos a otra → terminó.
            return [challenge["match_id"]], True

        return [challenge["match_id"]], False

    return None, False


async def update_challenges_progress(pool: asyncpg.Pool):
    """
    Recalcula el progreso de cada jugador para todos los desafíos activos.
    Soporta múltiples métricas por desafío (AND: todas deben cumplirse)
    y períodos por fecha o por partida (current_match / next_match).
    """
    async with pool.acquire() as conn:
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

            if ch["period"] in ("current_match", "next_match"):
                match_ids, should_close = await resolve_match_scope(conn, ch)
                if match_ids is None:
                    continue  # todavía no hay ninguna partida registrada

            metrics = await conn.fetch(
                "SELECT id, metric, target FROM challenge_metrics WHERE challenge_id = $1",
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
                    match_ids=match_ids, start_date=start_date, end_date=end_date
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
                    "UPDATE challenges SET active = FALSE, end_date = NOW() WHERE id = $1",
                    ch["id"]
                )
                log.info(f"  Desafío '{ch['name']}' (#{ch['id']}) cerrado: terminó la partida asociada")


async def run():
    log.info("Collector iniciado")
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
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
                await update_challenges_progress(pool)

            except Exception as e:
                log.error(f"Error en ciclo: {e}", exc_info=True)

            log.info(f"Próxima ejecución en {INTERVAL // 60} minutos")
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())