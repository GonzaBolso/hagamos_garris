"""crcon.py — Cliente HTTP para CRCON. Sin lógica de negocio."""
import logging
import aiohttp
import config

log = logging.getLogger(__name__)


async def _get(session: aiohttp.ClientSession, endpoint: str, **params) -> dict:
    url = f"{config.CRCON_URL}/api/{endpoint}"
    async with session.get(url, params=params or None) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"{endpoint} falló: {data.get('error')}")
        return data.get("result") or {}


async def _post(session: aiohttp.ClientSession, endpoint: str, body: dict) -> dict:
    url = f"{config.CRCON_URL}/api/{endpoint}"
    async with session.post(url, json=body) as resp:
        data = await resp.json()
        if data.get("failed"):
            raise RuntimeError(f"{endpoint} falló: {data.get('error')}")
        return data.get("result") or {}


async def fetch_scoreboard_maps(session: aiohttp.ClientSession, page: int = 1) -> dict:
    return await _get(session, "get_scoreboard_maps", page=page, page_size=100)


async def fetch_map_scoreboard(session: aiohttp.ClientSession, map_id: int) -> dict:
    return await _get(session, "get_map_scoreboard", map_id=map_id)


async def fetch_public_info(session: aiohttp.ClientSession) -> dict:
    return await _get(session, "get_public_info")


async def fetch_team_view(session: aiohttp.ClientSession) -> dict:
    return await _get(session, "get_team_view")


async def fetch_live_game_stats(session: aiohttp.ClientSession) -> dict:
    return await _get(session, "get_live_game_stats")


async def fetch_recent_logs(session: aiohttp.ClientSession,
                             limit: int = 500, action: str = "") -> list:
    """
    Trae los últimos N eventos del log. No tiene from/till — el caller
    es responsable de deduplicar por clave compuesta (ver make_event_key).
    """
    body = {
        "end": limit,
        "filter_action": [action] if action else [],
        "filter_player": [],
        "exact_action": bool(action),
        "inclusive_filter": True,
    }
    result = await _post(session, "get_recent_logs", body)
    return (result or {}).get("logs") or []


async def send_webhook(session: aiohttp.ClientSession, message: str) -> None:
    """Manda un mensaje al canal de status via webhook. No falla si no está configurado."""
    if not config.STATUS_WEBHOOK_URL:
        return
    try:
        async with session.post(
            config.STATUS_WEBHOOK_URL,
            json={"content": message},
            headers={"Content-Type": "application/json"},
        ) as resp:
            if resp.status not in (200, 204):
                log.warning(f"Webhook respondió {resp.status}")
    except Exception as e:
        log.warning(f"No se pudo mandar webhook: {e}")