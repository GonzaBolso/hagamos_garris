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

            except Exception as e:
                log.error(f"Error en ciclo: {e}", exc_info=True)

            log.info(f"Próxima ejecución en {INTERVAL // 60} minutos")
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())