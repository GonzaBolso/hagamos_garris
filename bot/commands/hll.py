"""
commands/hll.py
Grupo /hll con subcomandos: registro, help, server, online, top, vip, setchannel, setroles
"""
import re
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from api import crcon, CRCONError
from checks import admin_only, player_or_admin
from timeutils import parse_iso_to_local, format_local
from leaderboards import (
    TZ_UY, fetch_leaderboard, build_leaderboard_embed, HLL_WEAPONS, get_top_killers_by_weapon,
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


async def update_vinculados_message(bot, pool, guild_id: int):
    """
    Edita (o crea si no existe) el mensaje fijo con la lista de cuentas
    vinculadas Discord<->Steam, en el canal configurado con
    /hlladmin setchannel canal_vinculados:#canal. Se llama cada vez que
    alguien usa /hll registro, y al configurar el canal por primera vez.
    Ordenado por linked_at descendente (más reciente primero).
    """
    async with pool.acquire() as conn:
        config = await conn.fetchrow(
            "SELECT vinculados_channel_id, vinculados_message_id FROM guild_config WHERE guild_id = $1",
            guild_id
        )
        if not config or not config["vinculados_channel_id"]:
            return  # canal no configurado, nada que hacer

        rows = await conn.fetch(
            """
            SELECT discord_id, discord_name, steam_id, linked_at
            FROM linked_players
            ORDER BY linked_at DESC
            """
        )

    channel = bot.get_channel(config["vinculados_channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(config["vinculados_channel_id"])
        except discord.HTTPException:
            return

    if rows:
        lines = [
            f"`{r['discord_name'] or '?'}` — `{r['steam_id']}` "
            f"_(vinculado {format_local(r['linked_at'], '%d/%m/%Y %H:%M')})_"
            for r in rows
        ]
        description = "\n".join(lines)
    else:
        description = "_Todavía no hay nadie vinculado._"

    # Discord limita 'description' a 4096 caracteres; con muchos vinculados
    # esto podría no entrar — recortamos como salvaguarda, igual que
    # hacemos en /stats weapon.
    if len(description) > 4000:
        shown = []
        total_len = 0
        for line in lines:
            if total_len + len(line) + 1 > 3950:
                break
            shown.append(line)
            total_len += len(line) + 1
        faltantes = len(lines) - len(shown)
        description = "\n".join(shown) + f"\n\n_... y {faltantes} más_"

    embed = discord.Embed(
        title="🔗 Cuentas vinculadas (Discord ↔ Steam)",
        description=description,
        color=0x5865F2
    )
    embed.set_footer(text=f"{len(rows)} cuenta(s) vinculada(s) • Ordenado por más reciente")

    message_id = config["vinculados_message_id"]
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            return
        except discord.NotFound:
            pass  # el mensaje fue borrado a mano; mandamos uno nuevo abajo
        except discord.HTTPException:
            return

    new_message = await channel.send(embed=embed)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE guild_config SET vinculados_message_id = $1 WHERE guild_id = $2",
            new_message.id, guild_id
        )


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
        canal_snapshots="Canal donde se mandan los Top diarios/semanales/mensuales automáticos (opcional)",
        canal_desafios="Canal donde se manda la foto final cuando se cierra un desafío (opcional)",
        canal_vinculados="Canal privado con la lista de cuentas vinculadas Discord<->Steam, actualizada sola (opcional)"
    )
    @admin_only()
    async def setchannel(interaction: discord.Interaction,
                          canal: discord.TextChannel,
                          canal_snapshots: discord.TextChannel = None,
                          canal_desafios: discord.TextChannel = None,
                          canal_vinculados: discord.TextChannel = None):
        snapshots_id = canal_snapshots.id if canal_snapshots is not None else None
        desafios_id = canal_desafios.id if canal_desafios is not None else None
        vinculados_id = canal_vinculados.id if canal_vinculados is not None else None

        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO guild_config (guild_id, stats_channel_id, snapshot_channel_id, challenge_channel_id, vinculados_channel_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id) DO UPDATE
                    SET stats_channel_id = $2,
                        snapshot_channel_id = COALESCE($3, guild_config.snapshot_channel_id),
                        challenge_channel_id = COALESCE($4, guild_config.challenge_channel_id),
                        vinculados_channel_id = COALESCE($5, guild_config.vinculados_channel_id),
                        updated_at = NOW()
                """,
                interaction.guild_id, canal.id, snapshots_id, desafios_id, vinculados_id
            )

        msg = f"✅ Canal de jugadores configurado: {canal.mention}\nLos jugadores solo podrán usar comandos ahí."
        if canal_snapshots is not None:
            msg += f"\n✅ Canal de snapshots automáticos: {canal_snapshots.mention}"
        if canal_desafios is not None:
            msg += f"\n✅ Canal de cierre de desafíos: {canal_desafios.mention}"
        if canal_vinculados is not None:
            msg += f"\n✅ Canal de vinculados: {canal_vinculados.mention}"
        await interaction.response.send_message(msg, ephemeral=True)

        # Al configurar (o reconfigurar) el canal de vinculados, refrescamos
        # de una el mensaje fijo, así no queda vacío hasta el próximo /hll registro.
        if canal_vinculados is not None:
            await update_vinculados_message(interaction.client, pool, interaction.guild_id)

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
        challenge_channel = interaction.guild.get_channel(row["challenge_channel_id"]) if row.get("challenge_channel_id") else None
        vinculados_channel = interaction.guild.get_channel(row["vinculados_channel_id"]) if row.get("vinculados_channel_id") else None
        admin_role  = interaction.guild.get_role(row["admin_role_id"])  if row["admin_role_id"]  else None
        player_role = interaction.guild.get_role(row["mod_role_id"])    if row["mod_role_id"]    else None

        embed = discord.Embed(title="⚙️ Configuración del Bot", color=0x5865F2)
        embed.add_field(name="Canal jugadores", value=channel.mention  if channel     else "No configurado", inline=False)
        embed.add_field(name="Canal snapshots", value=snapshot_channel.mention if snapshot_channel else "No configurado", inline=False)
        embed.add_field(name="Canal desafíos",  value=challenge_channel.mention if challenge_channel else "No configurado", inline=False)
        embed.add_field(name="Canal vinculados", value=vinculados_channel.mention if vinculados_channel else "No configurado", inline=False)
        embed.add_field(name="Rol Admin",       value=admin_role.mention  if admin_role  else "No configurado", inline=True)
        embed.add_field(name="Rol Player",      value=player_role.mention if player_role else "No configurado", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hll registro ─────────────────────────────────────────
    @group.command(name="registro", description="Vinculá tu cuenta de Discord con tu Steam ID")
    @app_commands.describe(steam_id="Tu Steam ID de 64 bits (ej: 76561198XXXXXXXXX). Si no lo tenés, pedile tu ID a un admin")
    @player_or_admin()
    async def registro(interaction: discord.Interaction, steam_id: str):
        await interaction.response.defer(ephemeral=True)

        steam_id = steam_id.strip()
        es_steam64 = bool(re.fullmatch(r"\d{17}", steam_id))
        es_consola = bool(re.fullmatch(r"[0-9a-fA-F]{32}", steam_id))

        if not (es_steam64 or es_consola):
            await interaction.followup.send(
                "❌ ID inválido. Debe ser un Steam ID de 17 dígitos "
                "(encontralo en https://steamid.io) o un ID de consola de 32 caracteres.",
                ephemeral=True
            )
            return

        async with pool.acquire() as conn:
            player = await conn.fetchrow(
                "SELECT player_name FROM players WHERE steam_id = $1", steam_id
            )
            if not player:
                await interaction.followup.send(
                    "❌ Ese ID no aparece en nuestros registros — todavía no detectamos "
                    "ninguna partida tuya en el servidor. Jugá al menos una partida y "
                    "probá de nuevo en unos minutos.",
                    ephemeral=True
                )
                return

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
                INSERT INTO linked_players (discord_id, steam_id, discord_name, linked_at)
                VALUES ($1, $2, $3, NOW())
                ON CONFLICT (discord_id) DO UPDATE
                  SET steam_id = $2, discord_name = $3, linked_at = NOW()
                """,
                interaction.user.id, steam_id, str(interaction.user),
            )

        await interaction.followup.send(
            f"✅ Vinculado correctamente.\n"
            f"Discord: **{interaction.user}**\n"
            f"Steam: **{player['player_name'] or '?'}**\n"
            f"Steam ID: `{steam_id}`", ephemeral=True
        )

        await update_vinculados_message(interaction.client, pool, interaction.guild_id)

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
            nombre_vip = entry.get("name", "")
            vence_txt  = format_vip_expiration(entry.get("vip_expiration"))
            await interaction.followup.send(
                f"⭐ **Tenés VIP activo**\n"
                f"Nombre VIP: {nombre_vip or '—'}\n"
                f"Vence: {vence_txt}", ephemeral=True
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

    # ── Autocompletado: arma (lista estática, compartida con desafios) ──
    async def weapon_arma_autocomplete(interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [w for w in HLL_WEAPONS if current_lower in w.lower()]
        return [
            app_commands.Choice(name=w, value=w)
            for w in matches[:25]
        ]

    # ── /hll weapon ───────────────────────────────────────────
    @group.command(name="weapon", description="Top 10 de jugadores con más kills usando un arma específica")
    @app_commands.describe(arma="Arma exacta (ej: BAZOOKA, MP40, M2 AP MINE)")
    @app_commands.autocomplete(arma=weapon_arma_autocomplete)
    @player_or_admin()
    async def weapon(interaction: discord.Interaction, arma: str):
        await interaction.response.defer()

        rows = await get_top_killers_by_weapon(pool, arma, limit=10)

        if not rows:
            await interaction.followup.send(f"No hay kills registrados con **{arma}** todavía.")
            return

        lines = [
            f"`{i+1}.` **{r['player_name'] or r['steam_id']}** — {r['kills']} kills "
            f"· {r['matches']} partida{'s' if r['matches'] != 1 else ''}"
            for i, r in enumerate(rows)
        ]

        embed = discord.Embed(
            title=f"🔫 Top 10 — {arma}",
            description="\n".join(lines),
            color=0xE67E22
        )
        embed.set_footer(text="📊 Stats históricos acumulados")
        await interaction.followup.send(embed=embed)

    # ── /hlladmin snapshot ──────────────────────────────────────
    @admin_group.command(name="snapshot", description="Manda el resumen de Top 10 de un período (hoy, o una fecha puntual)")
    @app_commands.describe(
        periodo="Qué resumen mandar: día, semana o mes",
        fecha="Fecha de referencia en formato DD/MM/AAAA (ej: 20/06/2026). Si no se pasa, usa hoy/ahora."
    )
    @app_commands.choices(periodo=[
        app_commands.Choice(name="Día",    value="day"),
        app_commands.Choice(name="Semana", value="week"),
        app_commands.Choice(name="Mes",    value="month"),
    ])
    @admin_only()
    async def snapshot(interaction: discord.Interaction, periodo: app_commands.Choice[str] = None,
                        fecha: str = None):
        await interaction.response.defer(ephemeral=True)

        reference_date = None
        if fecha:
            try:
                parsed = datetime.strptime(fecha.strip(), "%d/%m/%Y")
                reference_date = parsed.replace(tzinfo=TZ_UY)
            except ValueError:
                await interaction.followup.send(
                    "❌ Formato de fecha inválido. Usá DD/MM/AAAA (ej: 20/06/2026).",
                    ephemeral=True
                )
                return

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
            interaction.client, pool, interaction.guild_id, row["snapshot_channel_id"],
            period_value, reference_date
        )
        channel_mention = f"<#{row['snapshot_channel_id']}>"
        await interaction.followup.send(f"✅ Snapshot enviado a {channel_mention}.", ephemeral=True)

    # ── /hll help ─────────────────────────────────────────────
    def _build_player_help_embed() -> discord.Embed:
        embed = discord.Embed(title="📖 Comandos HLL Bot", color=0x5865F2)
        embed.add_field(name="/hll registro <steam_id>", value="Vincula tu Discord con tu Steam ID", inline=False)
        embed.add_field(name="/hll perfil",              value="Tu perfil en CRCON (sesiones, horas, VIP)", inline=False)
        embed.add_field(name="/hll server",              value="Estado del servidor (mapa, jugadores, score)", inline=False)
        embed.add_field(name="/hll online",              value="Jugadores conectados ahora mismo", inline=False)
        embed.add_field(name="/hll vip",                 value="Verificá si tenés VIP activo", inline=False)
        embed.add_field(name="/hll top <categoria> [periodo]", value="Ranking: Kills, K/D, Partidas, etc. Período: histórico/día/semana/mes", inline=False)
        embed.add_field(name="/hll weapon <arma>", value="Top 10 de jugadores con más kills usando esa arma", inline=False)
        embed.add_field(name="/hll desafio listar",      value="Muestra los desafíos activos", inline=False)
        embed.add_field(name="/hll desafio progreso <id>", value="Ranking de progreso de un desafío", inline=False)
        embed.add_field(name="/stats show",              value="Tus stats acumulados", inline=False)
        embed.add_field(name="/stats games [cantidad]",  value="Tus últimas N partidas", inline=False)
        return embed

    @group.command(name="help", description="Lista de comandos disponibles")
    async def help_cmd(interaction: discord.Interaction):
        embed = _build_player_help_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hlladmin help ───────────────────────────────────────
    @admin_group.command(name="help", description="Lista de TODOS los comandos (jugador + admin)")
    @admin_only()
    async def admin_help_cmd(interaction: discord.Interaction):
        embed = _build_player_help_embed()
        embed.add_field(name="── Admin (/hlladmin) ──",            value="\u200b", inline=False)
        embed.add_field(name="/hlladmin snapshot [periodo]",       value="Manda ahora el resumen Top 10, sin esperar la hora programada", inline=False)
        embed.add_field(name="/hlladmin setchannel #canal",        value="Configura el canal para jugadores y/o snapshots", inline=False)
        embed.add_field(name="/hlladmin setroles @admin @player",  value="Configura los roles", inline=False)
        embed.add_field(name="/hlladmin config",                   value="Muestra la configuración actual", inline=False)
        embed.add_field(name="/hlladmin desafio metricas",         value="Lista las métricas disponibles para crear desafíos", inline=False)
        embed.add_field(name="/hlladmin desafio crear",            value="Crea un desafío configurable", inline=False)
        embed.add_field(name="/hlladmin desafio eliminar <id>",    value="Desactiva un desafío", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    return group, admin_group