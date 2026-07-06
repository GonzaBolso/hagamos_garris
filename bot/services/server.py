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
        "current_map":        state.get("current_map", {}).get("pretty_name", "?"),
        "next_map":           state.get("next_map", {}).get("pretty_name", "?"),
        "allied":             state.get("num_allied_players", 0),
        "axis":               state.get("num_axis_players", 0),
        "time_rem":           format_time_remaining(state.get("time_remaining")),
        "score_allied":       state.get("allied_score", 0),
        "score_axis":         state.get("axis_score", 0),
        "max_players":        (slots or {}).get("max_players", 100),
        "current_map_image":  state.get("current_map", {}).get("image_name"),
        "next_map_image":     state.get("next_map", {}).get("image_name"),
    }


def build_server_state_from_public_info(info: dict, slots: dict) -> dict:
    """
    Alternativa a build_server_state usando get_public_info, que tiene
    image_name disponible directamente en current_map/next_map.
    """
    current = (info or {}).get("current_map") or {}
    nxt     = (info or {}).get("next_map") or {}
    current_map_data = current.get("map") or {}
    next_map_data    = nxt.get("map") or {}
    score   = (info or {}).get("score") or {}
    by_team = (info or {}).get("player_count_by_team") or {}
    name_info   = (info or {}).get("name") or {}
    vote_status = (info or {}).get("vote_status") or []
    votes = [
        {
            "map_name": v.get("map", {}).get("pretty_name", "?"),
            "votes":    len(v.get("voters") or []),
        }
        for v in vote_status
    ]
    return {
        "current_map":        current_map_data.get("pretty_name", "?"),
        "next_map":           next_map_data.get("pretty_name", "?"),
        "allied":             by_team.get("allied", 0),
        "axis":               by_team.get("axis", 0),
        "time_rem":           format_time_remaining((info or {}).get("time_remaining")),
        "score_allied":       score.get("allied", 0),
        "score_axis":         score.get("axis", 0),
        "max_players":        (info or {}).get("max_player_count") or (slots or {}).get("max_players", 100),
        "current_map_image":  current_map_data.get("image_name"),
        "next_map_image":     next_map_data.get("image_name"),
        "server_name":        name_info.get("short_name") or name_info.get("name", ""),
        "votes":              votes,
    }


def build_perfil_data(data: dict) -> dict:
    """Extrae los campos del perfil de un jugador desde get_player_profile."""
    d           = data or {}
    # Horas: usar total_playtime_seconds del perfil (no sumar sesiones parciales)
    total_h     = round(d.get("total_playtime_seconds", 0) / 3600, 1)
    # Sesiones: usar sessions_count (total real, no solo las devueltas)
    sessions    = d.get("sessions_count") or len(d.get("sessions") or [])
    # Nivel: viene en soldier.level
    soldier     = d.get("soldier") or {}
    level       = soldier.get("level")
    platform    = soldier.get("platform", "")
    clan_tag    = soldier.get("clan_tag", "") or ""
    # VIP
    vips        = d.get("vips") or []
    is_vip      = d.get("is_vip") or bool(vips)
    vip_exp     = vips[0].get("expiration") if vips else None
    # Steam info
    steaminfo   = d.get("steaminfo") or {}
    profile     = (steaminfo.get("profile") or {})
    bans        = (steaminfo.get("bans") or {})

    return {
        "last_name":   d.get("names", [{}])[0].get("name", "?") if d.get("names") else "?",
        "clan_tag":    clan_tag,
        "level":       level,
        "platform":    platform,
        "sessions":    sessions,
        "total_h":     total_h,
        "is_vip":      is_vip,
        "vip_exp":     vip_exp,
        "avatar":      profile.get("avatarfull"),
        "country":     steaminfo.get("country"),
        "vac_banned":  bans.get("VACBanned", False),
    }


def build_server_status_embed(state: dict, slots: dict, players: list,
                               crcon_url: str = "") -> "discord.Embed":
    """
    Embed combinado de estado del servidor + jugadores online.
    Se usa para el panel que se edita en lugar cada 60s.
    crcon_url: base URL de CRCON para construir las URLs de imágenes de mapa.
    """
    import discord
    from datetime import datetime, timezone

    s = build_server_state_from_public_info(state, slots) if "current_map" in state and isinstance(state.get("current_map"), dict) and "map" in state.get("current_map", {}) else build_server_state(state, slots)
    total = s["allied"] + s["axis"]

    title = s.get("server_name") or "Estado del Servidor"
    embed = discord.Embed(
        title=title,
        color=0x57F287 if total > 0 else 0x99AAB5,
    )

    # Fila 1: mapa actual | próximo mapa (2 columnas)
    embed.add_field(name="🗺️ Mapa actual",  value=s["current_map"], inline=True)
    embed.add_field(name="⏭️ Próximo mapa", value=s["next_map"],    inline=True)

    # Fila 2: tiempo restante | score (2 columnas)
    embed.add_field(name="⏱️ Tiempo restante", value=s["time_rem"],                              inline=True)
    embed.add_field(name="🏆 Score",           value=f"Aliados {s['score_allied']} — {s['score_axis']} Eje", inline=True)

    # Votación del próximo mapa
    votes = s.get("votes") or []
    if votes:
        total_votes = sum(v["votes"] for v in votes)
        lines = []
        for v in sorted(votes, key=lambda x: x["votes"], reverse=True):
            bar = "█" * v["votes"] + "░" * (total_votes - v["votes"]) if total_votes > 0 else ""
            count = f"{v['votes']} voto{'s' if v['votes'] != 1 else ''}" if v["votes"] else "sin votos"
            lines.append(f"`{bar or '░░░░░░'}` {v['map_name']} — {count}")
        embed.add_field(name="🗳️ Votación próximo mapa", value="\n".join(lines), inline=False)

    if crcon_url and s.get("current_map_image"):
        embed.set_image(url=f"{crcon_url.rstrip('/')}/maps/{s['current_map_image']}")

    embed.timestamp = datetime.now(timezone.utc)
    return embed


def build_online_embed(players: list, allied: int = 0, axis: int = 0) -> "discord.Embed":
    """Embed de jugadores conectados, estilo /hll online."""
    import discord
    if not players:
        return discord.Embed(
            title="🔴 Sin jugadores",
            description="El servidor está vacío.",
            color=0x99AAB5,
        )
    names  = [p.get("name", "?") for p in players]
    mid    = (len(names) + 1) // 2
    col1   = names[:mid]
    col2   = names[mid:]
    total_p = len(players)
    embed  = discord.Embed(
        title=f"🟢 {total_p} jugador{'es' if total_p != 1 else ''} — Aliados: {allied} | Eje: {axis}",
        color=0x57F287,
    )
    embed.add_field(
        name="Jugadores",
        value="\n".join(f"• {n}" for n in col1),
        inline=True,
    )
    if col2:
        embed.add_field(
            name="\u200b",
            value="\n".join(f"• {n}" for n in col2),
            inline=True,
        )
    return embed


ROLE_ABBR = {
    "officer":           "OF",
    "spotter":           "SP",
    "rifleman":          "RI",
    "assault":           "AS",
    "automaticrifleman": "AR",
    "medic":             "ME",
    "support":           "SU",
    "heavymachinegunner":"MG",
    "antitank":          "AT",
    "engineer":          "EN",
    "tankcommander":     "TC",
    "crewman":           "CR",
    "sniper":            "SN",
    "armycommander":     "CO",
}

SQUAD_TYPE_EMOJI = {
    "infantry": "🪖",
    "recon":    "🎯",
    "armor":    "🪖",
    "armor":    "🔩",
    "command":  "⭐",
}


def build_team_view_embeds(team_view: dict, allied: int = 0, axis: int = 0) -> list:
    """
    Genera dos embeds (aliados y eje) con la composición de squads.
    Versión compacta: una línea por squad.
    """
    import discord

    def render_team(team_data: dict, label: str, color: int) -> "discord.Embed":
        embed = discord.Embed(title=label, color=color)
        squads = team_data.get("squads") or {}
        total  = team_data.get("count", 0)

        all_names = []
        commander = team_data.get("commander")
        if commander:
            all_names.append(f"⭐ {commander.get('name') or commander.get('player', '?')}")

        for squad_name, squad in sorted(squads.items()):
            for p in (squad.get("players") or []):
                all_names.append(p["name"])

        mid  = (len(all_names) + 1) // 2
        col1 = "\n".join(all_names[:mid]) or "—"
        col2 = "\n".join(all_names[mid:]) if all_names[mid:] else None

        embed.add_field(name="Jugadores", value=col1[:1024], inline=True)
        if col2:
            embed.add_field(name="\u200b", value=col2[:1024], inline=True)

        embed.set_footer(text=f"{total} jugadores")
        return embed

    allies_data = team_view.get("allies") or {}
    axis_data   = team_view.get("axis") or {}

    return [
        render_team(allies_data, f"🔵 Aliados — {allied} jugadores", 0x3498DB),
        render_team(axis_data,   f"🔴 Eje — {axis} jugadores",       0xE74C3C),
    ]