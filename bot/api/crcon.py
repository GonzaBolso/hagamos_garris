"""api/crcon.py — Cliente HTTP para CRCON, solo lectura."""
import logging
import aiohttp
import config

log = logging.getLogger(__name__)


class CRCONError(Exception):
    pass


class CRCONClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        log.info(f"CRCON URL: {config.CRCON_URL}")
        log.info(f"API KEY (primeros 8 chars): {config.CRCON_API_KEY[:8]}...")
        self._session = aiohttp.ClientSession(headers={
            "Authorization": f"bearer {config.CRCON_API_KEY}",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        })

    async def close(self):
        if self._session:
            await self._session.close()

    async def _get(self, endpoint: str, **params):
        url = f"{config.CRCON_URL}/api/{endpoint}"
        log.debug(f"GET {url} params={params}")
        async with self._session.get(url, params=params or None) as resp:
            data = await resp.json()
            log.debug(f"  -> failed={data.get('failed')} error={data.get('error')}")
            if data.get("failed"):
                raise CRCONError(data.get("error", endpoint))
            return data.get("result")

    async def _post(self, endpoint: str, **body):
        url = f"{config.CRCON_URL}/api/{endpoint}"
        async with self._session.post(url, json=body or {}) as resp:
            data = await resp.json()
            if data.get("failed"):
                raise CRCONError(data.get("error", endpoint))
            return data.get("result")

    # ── Servidor ──────────────────────────────────────────────
    async def get_gamestate(self):
        return await self._get("get_gamestate")

    async def get_slots(self):
        return await self._get("get_slots")

    async def get_map(self):
        return await self._get("get_map")

    async def get_next_map(self):
        return await self._get("get_next_map")

    # ── Jugadores ─────────────────────────────────────────────
    async def get_players(self):
        return await self._get("get_players")

    async def get_detailed_players(self):
        return await self._get("get_detailed_players")

    async def get_player_profile(self, steam_id: str, num_sessions: int = 10):
        return await self._get("get_player_profile", player_id=steam_id, num_sessions=num_sessions)

    async def get_players_history(self, player_id: str = None, player_name: str = None,
                                   page: int = 1, page_size: int = 10):
        params = {"page": page, "page_size": page_size}
        if player_id:
            params["player_id"] = player_id
        if player_name:
            params["player_name"] = player_name
        return await self._get("get_players_history", **params)

    # ── Stats ─────────────────────────────────────────────────
    async def get_live_game_stats(self):
        return await self._get("get_live_game_stats")

    async def get_live_scoreboard(self):
        return await self._get("get_live_scoreboard")

    async def get_scoreboard_maps(self):
        return await self._get("get_scoreboard_maps")

    # ── VIP ───────────────────────────────────────────────────
    async def get_vip_ids(self):
        return await self._get("get_vip_ids")


crcon = CRCONClient()