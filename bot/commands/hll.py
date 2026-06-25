"""
commands/hll.py
Grupo /hll con subcomandos: registro, help, server, online, top, vip, setchannel, setroles
"""
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from api import crcon, CRCONError
from checks import admin_only, player_or_admin
from timeutils import parse_iso_to_local
from leaderboards import (
    fetch_leaderboard, build_leaderboard_embed,
)
from snapshot_task import run_snapshot_manual


def country_to_flag(code: str) -> str:
    """Convierte un código ISO de país (ej: 'UY') a su emoji de bandera."""
    if not code or len(code) != 2:
        return ""
    code = code.upper()
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


def format_vip_expiration(expiration: str) -> str:
    """Convierte una fecha ISO de vencimiento VIP en texto legible en hora local (o 'permanente')."""
    if not expiration:
        return "Sin vencimiento"
    if expiration.startswith("3000"):
        return "Sin vencimiento (permanente)"
    return parse_iso_to_local(expiration, "%d/%m/%Y %H:%M")


def format_time_remaining(seconds) -> str:
    """Convierte segundos a formato Xh Ym."""
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def setup_hll(bot: commands.Bot, pool):
    group = app_commands.Group(name="hll", description="Comandos de Hell Let Loose")

    # Grupo separado para comandos de administración. default_permissions hace
    # que Discord OCULTE este grupo del autocompletado para cualquiera que no
    # tenga el permiso "Administrador" en el servidor (no solo que falle al
    # ejecutarlo: ni siquiera aparece en la lista de comandos).
    admin_group = app_commands.Group(
        name="hlladmin",
        description="Comandos de administración del bot HLL",
        default_permissions=discord.Permissions(administrator=True),
    )

    # ── /hlladmin setchannel ───────────────────────────────────
    @admin_group.command(name="setchannel", description="Configura los canales del bot")
    @app_commands.describe(
        canal="Canal donde los jugadores podrán usar los comandos",
        canal_snapshots="Canal donde se mandan los Top diarios/semanales/mensuales automáticos (opcional)"
    )
    @admin_only()
    async def setchannel(interaction: discord.Interaction,
                          canal: discord.TextChannel,
                          canal_snapshots: discord.TextChannel = None):
        async with pool.acquire() as conn:
            if canal_snapshots is not None:
                await conn.execute(
                    """
                    INSERT INTO guild_config (guild_id, stats_channel_id, snapshot_channel_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (guild_id) DO UPDATE
                        SET stats_channel_id = $2, snapshot_channel_id = $3, updated_at = NOW()
                    """,
                    interaction.guild_id, canal.id, canal_snapshots.id
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO guild_config (guild_id, stats_channel_id)
                    VALUES ($1, $2)
                    ON CONFLICT (guild_id) DO UPDATE SET stats_channel_id = $2, updated_at = NOW()
                    """,
                    interaction.guild_id, canal.id
                )

        msg = f"✅ Canal de jugadores configurado: {canal.mention}\nLos jugadores solo podrán usar comandos ahí."
        if canal_snapshots is not None:
            msg += f"\n✅ Canal de snapshots automáticos: {canal_snapshots.mention}"
        await interaction.response.send_message(msg, ephemeral=True)

    # ── /hlladmin setroles ──────────────────────────────────────
    @admin_group.command(name="setroles", description="Configura los roles de admin y player")
    @app_commands.describe(
        admin="Rol que puede usar todos los comandos en cualquier canal",
        player="Rol que puede usar comandos en el canal configurado"
    )
    @admin_only()
    async def setroles(interaction: discord.Interaction,
                       admin: discord.Role,
                       player: discord.Role):
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO guild_config (guild_id, admin_role_id, mod_role_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (guild_id) DO UPDATE
                    SET admin_role_id = $2, mod_role_id = $3, updated_at = NOW()
                """,
                interaction.guild_id, admin.id, player.id
            )
        await interaction.response.send_message(
            f"✅ Roles configurados:\n"
            f"Admin: {admin.mention}\n"
            f"Player: {player.mention}",
            ephemeral=True
        )

    # ── /hlladmin config ────────────────────────────────────────
    @admin_group.command(name="config", description="Muestra la configuración actual")
    @admin_only()
    async def config(interaction: discord.Interaction):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM guild_config WHERE guild_id = $1", interaction.guild_id
            )

        if not row:
            await interaction.response.send_message(
                "⚠️ No hay configuración todavía. Usá `/hlladmin setchannel` y `/hlladmin setroles`.",
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(row["stats_channel_id"]) if row["stats_channel_id"] else None
        snapshot_channel = interaction.guild.get_channel(row["snapshot_channel_id"]) if row.get("snapshot_channel_id") else None
        admin_role  = interaction.guild.get_role(row["admin_role_id"])  if row["admin_role_id"]  else None
        player_role = interaction.guild.get_role(row["mod_role_id"])    if row["mod_role_id"]    else None

        embed = discord.Embed(title="⚙️ Configuración del Bot", color=0x5865F2)
        embed.add_field(name="Canal jugadores", value=channel.mention  if channel     else "No configurado", inline=False)
        embed.add_field(name="Canal snapshots", value=snapshot_channel.mention if snapshot_channel else "No configurado", inline=False)
        embed.add_field(name="Rol Admin",       value=admin_role.mention  if admin_role  else "No configurado", inline=True)
        embed.add_field(name="Rol Player",      value=player_role.mention if player_role else "No configurado", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hll registro ─────────────────────────────────────────
    @group.command(name="registro", description="Vinculá tu cuenta de Discord con tu Steam ID")
    @app_commands.describe(steam_id="Tu Steam ID de 64 bits (ej: 76561198XXXXXXXXX)")
    @player_or_admin()
    async def registro(interaction: discord.Interaction, steam_id: str):
        await interaction.response.defer(ephemeral=True)

        if not steam_id.isdigit() or len(steam_id) != 17:
            await interaction.followup.send(
                "❌ Steam ID inválido. Debe tener 17 dígitos.\n"
                "Encontralo en: https://steamid.io", ephemeral=True
            )
            return

        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT discord_id FROM linked_players WHERE steam_id = $1", steam_id
            )
            if existing and existing["discord_id"] != interaction.user.id:
                await interaction.followup.send(
                    "❌ Ese Steam ID ya está vinculado a otra cuenta.", ephemeral=True
                )
                return

            await conn.execute(
                """
                INSERT INTO linked_players (discord_id, steam_id, discord_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (discord_id) DO UPDATE
                  SET steam_id = $2, discord_name = $3
                """,
                interaction.user.id, steam_id, str(interaction.user),
            )

        await interaction.followup.send(
            f"✅ Vinculado correctamente.\n"
            f"Discord: **{interaction.user}**\n"
            f"Steam ID: `{steam_id}`", ephemeral=True
        )

    # ── /hll perfil ───────────────────────────────────────────
    @group.command(name="perfil", description="Muestra tu perfil en CRCON")
    @player_or_admin()
    async def perfil(interaction: discord.Interaction):
        await interaction.response.defer()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT steam_id FROM linked_players WHERE discord_id = $1", interaction.user.id
            )
        if not row:
            await interaction.followup.send(
                "❌ No tenés tu Steam ID vinculado. Usá `/hll registro <steam_id>` primero."
            )
            return

        try:
            data = await crcon.get_player_profile(row["steam_id"])
        except CRCONError as e:
            await interaction.followup.send(f"❌ Error de API: {e}")
            return

        if not data:
            await interaction.followup.send("❌ Jugador no encontrado en CRCON.")
            return

        soldier   = data.get("soldier") or {}
        last_name = soldier.get("name") or (data.get("names") or [{}])[0].get("name", "?")
        level     = soldier.get("level")
        clan_tag  = soldier.get("clan_tag")

        sessions = data.get("sessions_count", 0)
        total_h  = round(data.get("total_playtime_seconds", 0) / 3600, 1)

        is_vip  = data.get("is_vip", False)
        vips    = data.get("vips") or []
        vip_exp = vips[0].get("expiration") if vips else None

        steaminfo  = data.get("steaminfo") or {}
        profile    = steaminfo.get("profile") or {}
        avatar     = profile.get("avatarfull")
        country    = steaminfo.get("country")
        bans       = steaminfo.get("bans") or {}
        vac_banned = bans.get("VACBanned", False)

        # Color dinámico: VIP dorado, baneado rojo, normal azul Discord
        color = 0xF1C40F if is_vip else (0xED4245 if vac_banned else 0x5865F2)

        display_name = f"[{clan_tag}] {last_name}" if clan_tag else last_name

        embed = discord.Embed(color=color)
        embed.set_author(name=display_name, icon_url=avatar)
        if avatar:
            embed.set_thumbnail(url=avatar)

        if is_vip and vip_exp:
            vence_txt = format_vip_expiration(vip_exp)
            embed.description = f"**⭐ VIP Activo** — vence: {vence_txt}"
        elif is_vip:
            embed.description = "**⭐ VIP Activo**"
        else:
            embed.description = "**Jugador**"

        embed.add_field(name="🆔 Steam ID",      value=f"`{row['steam_id']}`", inline=False)
        if level is not None:
            embed.add_field(name="🎖️ Nivel", value=str(level), inline=True)
        embed.add_field(name="⏱️ Horas jugadas", value=f"{total_h}h",        inline=True)
        embed.add_field(name="🔄 Sesiones",       value=str(sessions),        inline=True)
        if country:
            flag = country_to_flag(country)
            embed.add_field(name="🌍 País", value=f"{flag} {country}".strip(), inline=True)

        if vac_banned:
            embed.add_field(name="⚠️ Atención", value="Cuenta con VAC ban registrado", inline=False)

        embed.set_footer(
            text=f"Vinculado a {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url
        )
        await interaction.followup.send(embed=embed)

    # ── /hll server ───────────────────────────────────────────
    @group.command(name="server", description="Estado actual del servidor")
    @player_or_admin()
    async def server(interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            state = await crcon.get_gamestate()
            slots = await crcon.get_slots()
        except CRCONError as e:
            await interaction.followup.send(f"❌ Error: {e}")
            return

        current_map  = state.get("current_map", {}).get("pretty_name", "?")
        next_map     = state.get("next_map", {}).get("pretty_name", "?")
        allied       = state.get("num_allied_players", 0)
        axis         = state.get("num_axis_players", 0)
        time_rem     = format_time_remaining(state.get("time_remaining"))
        score_allied = state.get("allied_score", 0)
        score_axis   = state.get("axis_score", 0)
        max_players  = slots.get("max_players", 100) if slots else 100

        embed = discord.Embed(title="🖥️ Estado del Servidor", color=0x57F287)
        embed.add_field(name="🗺️ Mapa actual",    value=current_map, inline=False)
        embed.add_field(name="⏭️ Próximo mapa",   value=next_map,    inline=False)
        embed.add_field(name="👥 Jugadores",
                        value=f"{allied + axis}/{max_players} (Aliados: {allied} | Eje: {axis})",
                        inline=False)
        embed.add_field(name="🏆 Score",
                        value=f"Aliados {score_allied} — {score_axis} Eje", inline=True)
        embed.add_field(name="⏱️ Tiempo restante", value=time_rem, inline=True)
        await interaction.followup.send(embed=embed)

    # ── /hll online ───────────────────────────────────────────
    @group.command(name="online", description="Jugadores conectados ahora")
    @player_or_admin()
    async def online(interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            players = await crcon.get_players()
        except CRCONError as e:
            await interaction.followup.send(f"❌ Error: {e}")
            return

        if not players:
            await interaction.followup.send("No hay jugadores conectados.")
            return

        names  = [p.get("name", "?") for p in players[:50]]
        chunks = [names[i:i+25] for i in range(0, len(names), 25)]

        embed = discord.Embed(title=f"🟢 {len(players)} jugadores conectados", color=0x57F287)
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name="\u200b" if i > 0 else "Jugadores",
                value="\n".join(f"• {n}" for n in chunk),
                inline=True
            )
        await interaction.followup.send(embed=embed)

    # ── /hll vip ──────────────────────────────────────────────
    @group.command(name="vip", description="Verificá si tenés VIP")
    @player_or_admin()
    async def vip(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT steam_id FROM linked_players WHERE discord_id = $1", interaction.user.id
            )
        if not row:
            await interaction.followup.send(
                "❌ Vinculá tu Steam ID primero con `/hll registro`.", ephemeral=True
            )
            return

        try:
            vips = await crcon.get_vip_ids()
        except CRCONError as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            return

        entry = next((v for v in (vips or []) if v.get("player_id") == row["steam_id"]), None)
        if entry:
            desc    = entry.get("description", "")
            expires = entry.get("expiration") or "Sin vencimiento"
            await interaction.followup.send(
                f"⭐ **Tenés VIP activo**\n"
                f"Descripción: {desc or '—'}\n"
                f"Vence: {expires}", ephemeral=True
            )
        else:
            await interaction.followup.send("❌ No tenés VIP en este servidor.", ephemeral=True)

    # ── /hll top ──────────────────────────────────────────────
    @group.command(name="top", description="Ranking de jugadores")
    @app_commands.describe(
        categoria="Qué querés rankear",
        cantidad="Cuántos jugadores mostrar (máx 20)",
        periodo="Rango de tiempo a considerar (default: histórico)"
    )
    @app_commands.choices(
        categoria=[
            app_commands.Choice(name="Kills",    value="total_kills"),
            app_commands.Choice(name="K/D",      value="kd_ratio"),
            app_commands.Choice(name="Partidas", value="matches_played"),
            app_commands.Choice(name="Combat",   value="total_combat"),
            app_commands.Choice(name="Offense",  value="total_offense"),
            app_commands.Choice(name="Defense",  value="total_defense"),
            app_commands.Choice(name="Support",  value="total_support"),
        ],
        periodo=[
            app_commands.Choice(name="Histórico", value="all"),
            app_commands.Choice(name="Día",       value="day"),
            app_commands.Choice(name="Semana",    value="week"),
            app_commands.Choice(name="Mes",       value="month"),
        ],
    )
    @player_or_admin()
    async def top(interaction: discord.Interaction,
                  categoria: app_commands.Choice[str],
                  cantidad: int = 10,
                  periodo: app_commands.Choice[str] = None):
        await interaction.response.defer()

        cantidad = max(1, min(cantidad, 20))
        col = categoria.value
        period_value = periodo.value if periodo else "all"

        rows = await fetch_leaderboard(pool, col, period_value, cantidad)

        if not rows:
            label = periodo.name if periodo else "Histórico"
            await interaction.followup.send(f"No hay datos para ese período ({label}).")
            return

        embed = build_leaderboard_embed(rows, col, categoria.name, period_value, cantidad)
        await interaction.followup.send(embed=embed)

    # ── /hlladmin snapshot ──────────────────────────────────────
    @admin_group.command(name="snapshot", description="Manda ahora mismo el resumen de Top 10 (sin esperar la hora programada)")
    @app_commands.describe(periodo="Qué resumen mandar: día, semana o mes")
    @app_commands.choices(periodo=[
        app_commands.Choice(name="Día",    value="day"),
        app_commands.Choice(name="Semana", value="week"),
        app_commands.Choice(name="Mes",    value="month"),
    ])
    @admin_only()
    async def snapshot(interaction: discord.Interaction, periodo: app_commands.Choice[str] = None):
        await interaction.response.defer(ephemeral=True)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT snapshot_channel_id FROM guild_config WHERE guild_id = $1", interaction.guild_id
            )

        if not row or not row["snapshot_channel_id"]:
            await interaction.followup.send(
                "❌ No hay canal de snapshots configurado. Usá `/hlladmin setchannel` con el parámetro `canal_snapshots`.",
                ephemeral=True
            )
            return

        period_value = periodo.value if periodo else "day"
        await run_snapshot_manual(
            interaction.client, pool, interaction.guild_id, row["snapshot_channel_id"], period_value
        )
        channel_mention = f"<#{row['snapshot_channel_id']}>"
        await interaction.followup.send(f"✅ Snapshot enviado a {channel_mention}.", ephemeral=True)

    # ── /hll help ─────────────────────────────────────────────
    @group.command(name="help", description="Lista de comandos disponibles")
    async def help_cmd(interaction: discord.Interaction):
        embed = discord.Embed(title="📖 Comandos HLL Bot", color=0x5865F2)
        embed.add_field(name="/hll registro <steam_id>", value="Vincula tu Discord con tu Steam ID", inline=False)
        embed.add_field(name="/hll perfil",              value="Tu perfil en CRCON (sesiones, horas, VIP)", inline=False)
        embed.add_field(name="/hll server",              value="Estado del servidor (mapa, jugadores, score)", inline=False)
        embed.add_field(name="/hll online",              value="Jugadores conectados ahora mismo", inline=False)
        embed.add_field(name="/hll vip",                 value="Verificá si tenés VIP activo", inline=False)
        embed.add_field(name="/hll top <categoria> [periodo]", value="Ranking: Kills, K/D, Partidas, etc. Período: histórico/día/semana/mes", inline=False)
        embed.add_field(name="/stats show",              value="Tus stats acumulados", inline=False)
        embed.add_field(name="/stats games [cantidad]",  value="Tus últimas N partidas", inline=False)

        # La sección admin solo se muestra a quien efectivamente tiene
        # permiso de Administrador (los comandos viven en /hlladmin, que
        # Discord ya oculta del autocompletado para el resto).
        if interaction.user.guild_permissions.administrator:
            embed.add_field(name="── Admin (/hlladmin) ──",        value="\u200b", inline=False)
            embed.add_field(name="/hlladmin snapshot [periodo]",   value="Manda ahora el resumen Top 10, sin esperar la hora programada", inline=False)
            embed.add_field(name="/hlladmin setchannel #canal",    value="Configura el canal para jugadores y/o snapshots", inline=False)
            embed.add_field(name="/hlladmin setroles @admin @player", value="Configura los roles", inline=False)
            embed.add_field(name="/hlladmin config",               value="Muestra la configuración actual", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    return group, admin_group