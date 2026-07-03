"""services/server.py — Lógica de comandos en vivo que consultan CRCON."""
from timeutils import parse_iso_to_local


def country_to_flag(code: str) -> str:
    if not code or len(code) != 2:
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


def format_vip_expiration(expiration: str) -> str:
    if not expiration:
        return "Sin vencimiento"
    if expiration.startswith("3000"):
        return "Sin vencimiento (permanente)"
    return parse_iso_to_local(expiration, "%d/%m/%Y %H:%M")


def format_time_remaining(seconds) -> str:
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"


def build_server_state(state: dict, slots: dict) -> dict:
    """Extrae los campos relevantes de get_gamestate y get_slots."""
    return {
        "current_map":  state.get("current_map", {}).get("pretty_name", "?"),
        "next_map":     state.get("next_map", {}).get("pretty_name", "?"),
        "allied":       state.get("num_allied_players", 0),
        "axis":         state.get("num_axis_players", 0),
        "time_rem":     format_time_remaining(state.get("time_remaining")),
        "score_allied": state.get("allied_score", 0),
        "score_axis":   state.get("axis_score", 0),
        "max_players":  (slots or {}).get("max_players", 100),
    }


def build_perfil_data(data: dict) -> dict:
    """Extrae los campos del perfil de un jugador desde get_player_profile."""
    sessions    = (data or {}).get("sessions") or []
    total_h     = round(sum(s.get("total_playtime_seconds", 0) for s in sessions) / 3600, 1)
    vip_info    = (data or {}).get("vip_status") or {}
    is_vip      = bool(vip_info.get("is_vip"))
    vip_exp     = vip_info.get("expiration")
    steam_info  = (data or {}).get("steam_info") or {}
    profile     = steam_info.get("profile") or {}
    bans        = steam_info.get("bans") or {}

    return {
        "last_name":   (data or {}).get("names", [{}])[0].get("name", "?") if (data or {}).get("names") else "?",
        "clan_tag":    (data or {}).get("clan_tag") or "",
        "level":       (data or {}).get("current_playtime_seconds"),  # placeholder
        "sessions":    len(sessions),
        "total_h":     total_h,
        "is_vip":      is_vip,
        "vip_exp":     vip_exp,
        "avatar":      profile.get("avatarfull"),
        "country":     steam_info.get("country"),
        "vac_banned":  bans.get("VACBanned", False),
    }