"""
leaderboards.py — Re-exporta desde services/leaderboard.py para compatibilidad.
Los archivos que todavía importan de aquí siguen funcionando sin cambios.
"""
from services.leaderboard import (
    TZ_UY,
    CATEGORIES,
    CATEGORY_BY_COLUMN,
    SNAPSHOT_CATEGORIES,
    PERIOD_LABELS,
    FOOTER_PERIOD_TEXT,
    HLL_WEAPONS,
    steam_profile_link,
    period_start,
    period_end,
    fetch_leaderboard,
    get_player_ranks,
    get_top_killers_by_weapon,
    get_all_weapons_with_rank,
    build_leaderboard_embed,
    build_all_category_embeds,
)

__all__ = [
    "TZ_UY", "CATEGORIES", "CATEGORY_BY_COLUMN", "SNAPSHOT_CATEGORIES",
    "PERIOD_LABELS", "FOOTER_PERIOD_TEXT", "HLL_WEAPONS",
    "steam_profile_link", "period_start", "period_end",
    "fetch_leaderboard", "get_player_ranks", "get_top_killers_by_weapon",
    "get_all_weapons_with_rank", "build_leaderboard_embed", "build_all_category_embeds",
]