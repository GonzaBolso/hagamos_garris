"""
Collector — corre cada COLLECT_INTERVAL_MINUTES minutos.
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

CRCON_URL     = os.environ["CRCON_URL"].rstrip("/")
CRCON_API_KEY = os.environ["CRCON_API_KEY"]
INTERVAL      = int(os.environ.get("COLLECT_INTERVAL_MINUTES", 30)) * 60

DB_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

HEADERS = {
    "Authorization": f"Bearer {CRCON_API_KEY}",
    "Content-Type": "application/json",
}


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


async def process_maps(pool: asyncpg.Pool, maps: list) -> int:
    new_count = 0

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
            result       = m.get("result") or {}
            allied_score = result.get("allied")
            axis_score   = result.get("axis")

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

            players = m.get("player_stats") or []
            for p in players:
                steam_id = p.get("player_id") or p.get("steam_id_64", "")
                if not steam_id:
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
                    p.get("player") or p.get("name", ""),
                    int(p.get("kills", 0)),
                    int(p.get("deaths", 0)),
                    int(p.get("teamkills", 0) or p.get("team_kills", 0)),
                    int(p.get("combat", 0)    or p.get("combat_score", 0)),
                    int(p.get("offense", 0)   or p.get("offense_score", 0)),
                    int(p.get("defense", 0)   or p.get("defense_score", 0)),
                    int(p.get("support", 0)   or p.get("support_score", 0)),
                    int(p.get("time_seconds", 0)),
                )

            new_count += 1
            log.info(f"  Nueva: [{match_id}] {map_name} ({allied_score}-{axis_score})")

    return new_count


async def run():
    log.info("Collector iniciado")
    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)

    async with aiohttp.ClientSession(headers=HEADERS) as session:
        while True:
            try:
                log.info("Consultando get_scoreboard_maps...")
                result = await fetch_scoreboard_maps(session, page=1)
                maps   = result.get("maps", [])
                total  = result.get("total", 0)
                log.info(f"  {total} partidas en CRCON, procesando página 1 ({len(maps)} partidas)...")
                new = await process_maps(pool, maps)
                log.info(f"  {new} partidas nuevas guardadas")
            except Exception as e:
                log.error(f"Error en ciclo: {e}", exc_info=True)

            log.info(f"Próxima ejecución en {INTERVAL // 60} minutos")
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(run())