"""services/stats.py — Lógica de stats individuales de jugadores."""
from db import players as db_players
from db import matches as db_matches
from services import leaderboard as lb_service


async def get_player_stats(pool, discord_id: int) -> tuple:
    """
    Devuelve (steam_id, totals, ranks, top_weapons) para el jugador vinculado
    al discord_id dado. Devuelve (None, None, None, None) si no está vinculado
    o no tiene partidas.
    """
    async with pool.acquire() as conn:
        steam_id = await db_players.get_linked_steam_id(conn, discord_id)
        if not steam_id:
            return None, None, None, None

        totals = await db_players.get_player_totals(conn, steam_id)
        if not totals:
            return steam_id, None, None, None

        top_weapons = await db_matches.get_top_weapons_for_player(conn, steam_id, limit=5)

    ranks = await lb_service.get_player_ranks(pool, steam_id)
    return steam_id, totals, ranks, top_weapons


async def get_player_recent_games(pool, discord_id: int, cantidad: int) -> tuple:
    """
    Devuelve (steam_id, matches) para el jugador vinculado.
    """
    async with pool.acquire() as conn:
        steam_id = await db_players.get_linked_steam_id(conn, discord_id)
        if not steam_id:
            return None, None
        matches = await db_matches.get_recent_matches_for_player(conn, steam_id, cantidad)
    return steam_id, matches


async def get_player_weapons(pool, discord_id: int) -> tuple:
    """
    Devuelve (steam_id, weapons_with_rank) para el jugador vinculado.
    """
    async with pool.acquire() as conn:
        steam_id = await db_players.get_linked_steam_id(conn, discord_id)
        if not steam_id:
            return None, None
        weapons = await db_matches.get_all_weapons_with_rank(conn, steam_id)
    return steam_id, weapons