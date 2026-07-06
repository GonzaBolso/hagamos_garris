"""
main.py — Entry point del collector.
Solo contiene los loops async y el bootstrap.
Toda la lógica está en service.py, db.py y crcon.py.
"""
import asyncio
import logging

import aiohttp
import asyncpg

import config
import crcon
import db
import service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [collector] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


async def main_collector_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession) -> None:
    """Cada COLLECT_INTERVAL_MINUTES: trae partidas nuevas y actualiza progreso de desafíos."""
    while True:
        try:
            live_map_start_epoch = None
            try:
                info = await crcon.fetch_public_info(session)
                current_map = (info or {}).get("current_map") or {}
                live_map_start_epoch = current_map.get("start")
            except Exception as e:
                log.warning(f"  No se pudo obtener get_public_info: {e}")

            total_new = 0
            page = 1
            while True:
                log.info(f"Consultando get_scoreboard_maps página {page}...")
                result    = await crcon.fetch_scoreboard_maps(session, page=page)
                maps      = result.get("maps", [])
                total     = result.get("total", 0)
                page_size = result.get("page_size", 100)

                if not maps:
                    break

                log.info(f"  Página {page}: {len(maps)} partidas (total CRCON: {total})")
                new = await service.process_maps(pool, session, maps,
                                                  live_map_start_epoch=live_map_start_epoch)
                total_new += new

                if new == 0:
                    log.info("  No hay más partidas nuevas, deteniendo paginación")
                    break

                if page * page_size >= total:
                    break

                page += 1

            log.info(f"Total partidas nuevas guardadas: {total_new}")
            log.info("Actualizando progreso de desafíos...")
            await service.update_challenges_progress(pool, session)

        except Exception as e:
            log.error(f"Error en ciclo: {e}", exc_info=True)
            await crcon.send_webhook(session,
                f"⚠️ **Error en collector** (ciclo principal)\n```{type(e).__name__}: {e}```")

        log.info(f"Próxima ejecución en {config.INTERVAL // 60} minutos")
        await asyncio.sleep(config.INTERVAL)


async def live_polling_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession) -> None:
    """Cada LIVE_POLL_INTERVAL_SECONDS: actualiza progreso en vivo de desafíos."""
    log.info(f"Live polling iniciado (cada {config.LIVE_POLL_INTERVAL_SECONDS}s)")
    while True:
        try:
            await service.run_live_progress_update(pool, session)
        except Exception as e:
            log.error(f"Error en live_polling_loop: {e}", exc_info=True)
            await crcon.send_webhook(session,
                f"⚠️ **Error en collector** (live polling)\n```{type(e).__name__}: {e}```")
        await asyncio.sleep(config.LIVE_POLL_INTERVAL_SECONDS)


async def map_bounds_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession) -> None:
    """Cada 60s: acumula world_position de jugadores para calibrar los bounds de cada mapa."""
    log.info("Map bounds collector iniciado (cada 60s)")
    while True:
        try:
            info      = await crcon.fetch_public_info(session)
            current   = (info or {}).get("current_map") or {}
            map_data  = current.get("map") or {}
            map_id    = map_data.get("id")

            if map_id:
                team_view = await crcon.fetch_team_view(session)
                positions = []
                for team_key in ("allies", "axis"):
                    team = (team_view or {}).get(team_key) or {}
                    for squad in (team.get("squads") or {}).values():
                        for player in (squad.get("players") or []):
                            wp = player.get("world_position") or {}
                            if wp.get("x") is not None:
                                positions.append({"x": wp["x"], "y": wp["y"]})

                if positions:
                    async with pool.acquire() as conn:
                        await db.update_map_bounds(conn, map_id, positions)

        except Exception as e:
            log.warning(f"Error en map_bounds_loop: {e}")
        await asyncio.sleep(60)


async def event_detector_loop(pool: asyncpg.Pool, session: aiohttp.ClientSession) -> None:
    """Cada EVENT_DETECTOR_INTERVAL_SECONDS: detecta fakeos y otros eventos destacados."""
    log.info(f"Detector de eventos iniciado (cada {config.EVENT_DETECTOR_INTERVAL_SECONDS}s)")
    while True:
        try:
            await service.detect_and_notify_events(pool, session)
        except Exception as e:
            log.error(f"Error en event_detector_loop: {e}", exc_info=True)
            await crcon.send_webhook(session,
                f"⚠️ **Error en collector** (detector de eventos)\n```{type(e).__name__}: {e}```")
        await asyncio.sleep(config.EVENT_DETECTOR_INTERVAL_SECONDS)


async def run() -> None:
    log.info("Collector iniciado")
    pool = await asyncpg.create_pool(config.DB_DSN, min_size=1, max_size=3)

    async with aiohttp.ClientSession(headers=config.HEADERS) as session:
        if config.BACKFILL_MATCH_STATS:
            await service.backfill_match_player_stats(pool, session)
            log.info("Backfill terminado, el proceso va a salir ahora.")
            return

        await asyncio.gather(
            main_collector_loop(pool, session),
            live_polling_loop(pool, session),
            event_detector_loop(pool, session),
            map_bounds_loop(pool, session),
        )


if __name__ == "__main__":
    asyncio.run(run())