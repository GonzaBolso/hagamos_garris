"""
commands/hll.py — Comandos /hll y /hlladmin.
Solo Discord: interacciones, embeds, respuestas.
Lógica en services/, queries en db/.
"""
import io
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from api import crcon, CRCONError
from checks import admin_only, player_or_admin
from db import guild as db_guild
from db import matches as db_matches
from services import guild as guild_service
from services import server as server_service
from services.leaderboard import (
    TZ_UY, HLL_WEAPONS, fetch_leaderboard, build_leaderboard_embed, get_top_killers_by_weapon,
)
from snapshot_task import run_snapshot_manual


def setup_hll(bot: commands.Bot, pool):
    group = app_commands.Group(name="hll", description="Comandos de Hell Let Loose")
    admin_group = app_commands.Group(
        name="hlladmin",
        description="Comandos de administración del bot HLL",
    )

    # ── /hlladmin setchannel ───────────────────────────────────
    @admin_group.command(name="setchannel", description="Configura los canales del bot")
    @app_commands.describe(
        canal="Canal donde los jugadores podrán usar los comandos",
        canal_snapshots="Canal para los Top diarios/semanales/mensuales automáticos",
        canal_desafios="Canal donde se manda el cierre de desafíos",
        canal_vinculados="Canal privado con la lista de cuentas vinculadas",
        canal_eventos="Canal para eventos destacados en vivo (fakeos, etc.)",
        canal_status="Canal con panel de estado del servidor actualizado automáticamente"
    )
    @admin_only()
    async def setchannel(interaction: discord.Interaction,
                          canal: discord.TextChannel = None,
                          canal_snapshots: discord.TextChannel = None,
                          canal_desafios: discord.TextChannel = None,
                          canal_vinculados: discord.TextChannel = None,
                          canal_eventos: discord.TextChannel = None,
                          canal_status: discord.TextChannel = None):
        if not any([canal, canal_snapshots, canal_desafios,
                    canal_vinculados, canal_eventos, canal_status]):
            await interaction.response.send_message(
                "❌ Tenés que especificar al menos un canal.", ephemeral=True
            )
            return

        async with pool.acquire() as conn:
            await db_guild.upsert_channels(
                conn, interaction.guild_id,
                canal.id            if canal            else None,
                canal_snapshots.id  if canal_snapshots  else None,
                canal_desafios.id   if canal_desafios   else None,
                canal_vinculados.id if canal_vinculados else None,
                canal_eventos.id    if canal_eventos    else None,
                canal_status.id     if canal_status     else None,
            )

        lines = ["✅ Canales actualizados:"]
        if canal:           lines.append(f"• Jugadores: {canal.mention}")
        if canal_snapshots: lines.append(f"• Snapshots: {canal_snapshots.mention}")
        if canal_desafios:  lines.append(f"• Desafíos: {canal_desafios.mention}")
        if canal_vinculados:lines.append(f"• Vinculados: {canal_vinculados.mention}")
        if canal_eventos:   lines.append(f"• Eventos: {canal_eventos.mention}")
        if canal_status:    lines.append(f"• Estado servidor: {canal_status.mention}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

        if canal_vinculados:
            await guild_service.update_vinculados_message(interaction.client, pool, interaction.guild_id)

    # ── /hlladmin setroles ──────────────────────────────────────
    @admin_group.command(name="setroles", description="Configura los roles de admin y player")
    @app_commands.describe(
        admin="Rol que puede usar todos los comandos en cualquier canal",
        player="Rol que puede usar comandos en el canal configurado"
    )
    @admin_only()
    async def setroles(interaction: discord.Interaction,
                        admin: discord.Role, player: discord.Role):
        async with pool.acquire() as conn:
            await db_guild.upsert_roles(conn, interaction.guild_id, admin.id, player.id)
        await interaction.response.send_message(
            f"✅ Roles configurados:\nAdmin: {admin.mention}\nPlayer: {player.mention}",
            ephemeral=True,
        )

    # ── /hlladmin seed ───────────────────────────────────────────
    @admin_group.command(name="seed", description="Configura la notificación de seedeo")
    @app_commands.describe(
        canal="Canal donde se manda el aviso de seed",
        umbral="Cantidad de jugadores para disparar la notificación (ej: 40)",
        rol="Rol a taggear en el aviso (opcional)",
    )
    @admin_only()
    async def seed(interaction: discord.Interaction,
                   canal: discord.TextChannel,
                   umbral: int,
                   rol: discord.Role = None):
        async with pool.acquire() as conn:
            await db_guild.set_seed_config(
                conn, interaction.guild_id,
                role_id=rol.id if rol else None,
                channel_id=canal.id,
                threshold=umbral,
            )
        msg = f"✅ Seed configurado:\nCanal: {canal.mention}\nUmbral: {umbral} jugadores"
        if rol:
            msg += f"\nRol: {rol.mention}"
        await interaction.response.send_message(msg, ephemeral=True)

    # ── /hlladmin config ────────────────────────────────────────
    @admin_group.command(name="config", description="Muestra la configuración actual")
    @admin_only()
    async def config(interaction: discord.Interaction):
        async with pool.acquire() as conn:
            row = await db_guild.get_guild_config(conn, interaction.guild_id)

        if not row:
            await interaction.response.send_message(
                "⚠️ No hay configuración todavía. Usá `/hlladmin setchannel` y `/hlladmin setroles`.",
                ephemeral=True,
            )
            return

        def ch(col):
            cid = row.get(col)
            return interaction.guild.get_channel(cid).mention if cid else "No configurado"

        def role(col):
            rid = row.get(col)
            return interaction.guild.get_role(rid).mention if rid else "No configurado"

        embed = discord.Embed(title="⚙️ Configuración del Bot", color=0x5865F2)
        embed.add_field(name="Canal jugadores",  value=ch("stats_channel_id"),      inline=False)
        embed.add_field(name="Canal snapshots",  value=ch("snapshot_channel_id"),   inline=False)
        embed.add_field(name="Canal desafíos",   value=ch("challenge_channel_id"),  inline=False)
        embed.add_field(name="Canal vinculados", value=ch("vinculados_channel_id"), inline=False)
        embed.add_field(name="Canal eventos",    value=ch("eventos_channel_id"),    inline=False)
        embed.add_field(name="Rol Admin",        value=role("admin_role_id"),       inline=True)
        embed.add_field(name="Rol Player",       value=role("mod_role_id"),         inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hll registro ─────────────────────────────────────────
    @group.command(name="registro", description="Vinculá tu cuenta de Discord con tu Steam ID")
    @app_commands.describe(steam_id="Tu Steam ID de 64 bits (ej: 76561198XXXXXXXXX)")
    @player_or_admin()
    async def registro(interaction: discord.Interaction, steam_id: str):
        await interaction.response.defer(ephemeral=True)

        steam_id = steam_id.strip()
        if not (re.fullmatch(r"\d{17}", steam_id) or re.fullmatch(r"[0-9a-fA-F]{32}", steam_id)):
            await interaction.followup.send(
                "❌ ID inválido. Debe ser un Steam ID de 17 dígitos "
                "(encontralo en https://steamid.io) o un ID de consola de 32 caracteres.",
                ephemeral=True,
            )
            return

        success, result = await guild_service.register_player(
            pool, interaction.user.id, str(interaction.user), steam_id
        )

        if not success:
            await interaction.followup.send(result, ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Vinculado correctamente.\n"
            f"Discord: **{interaction.user}**\n"
            f"Steam: **{result}**\n"
            f"Steam ID: `{steam_id}`",
            ephemeral=True,
        )
        await guild_service.update_vinculados_message(interaction.client, pool, interaction.guild_id)

    # ── /hll perfil ───────────────────────────────────────────
    @group.command(name="perfil", description="Muestra tu perfil en CRCON")
    @player_or_admin()
    async def perfil(interaction: discord.Interaction):
        await interaction.response.defer()

        async with pool.acquire() as conn:
            steam_id = await db_guild.get_linked_steam_id_for_discord(conn, interaction.user.id)

        if not steam_id:
            await interaction.followup.send(
                "❌ No tenés tu Steam ID vinculado. Usá `/hll registro <steam_id>` primero."
            )
            return

        try:
            data = await crcon.get_player_profile(steam_id)
        except CRCONError as e:
            await interaction.followup.send(f"❌ Error al obtener perfil: {e}")
            return

        p = server_service.build_perfil_data(data)
        color = 0xF1C40F if p["is_vip"] else (0xED4245 if p["vac_banned"] else 0x5865F2)
        display_name = f"[{p['clan_tag']}] {p['last_name']}" if p["clan_tag"] else p["last_name"]

        embed = discord.Embed(color=color)
        embed.set_author(name=display_name, icon_url=p["avatar"])
        if p["avatar"]:
            embed.set_thumbnail(url=p["avatar"])

        if p["is_vip"]:
            vence = server_service.format_vip_expiration(p["vip_exp"]) if p["vip_exp"] else ""
            embed.description = f"**⭐ VIP Activo**" + (f" — vence: {vence}" if vence else "")
        else:
            embed.description = "**Jugador**"

        embed.add_field(name="🆔 Steam ID",      value=f"`{steam_id}`",     inline=False)
        if p["level"]:
            platform_emoji = "🎮" if p.get("platform") == "epic" else "💨"
            embed.add_field(name=f"{platform_emoji} Nivel", value=str(p["level"]), inline=True)

        # Si CRCON no tiene datos de horas (Epic players), usar stats de la BD
        total_h  = p["total_h"]
        sessions = p["sessions"]
        if total_h == 0 or sessions == 0:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(DISTINCT match_id) AS partidas,
                           SUM(time_seconds) AS segundos
                    FROM match_player_stats
                    WHERE steam_id = $1
                    """,
                    steam_id,
                )
            if row and row["partidas"]:
                if sessions == 0:
                    sessions = row["partidas"]
                if total_h == 0 and row["segundos"]:
                    total_h = round(row["segundos"] / 3600, 1)

        embed.add_field(name="⏱️ Horas jugadas", value=f"{total_h}h",  inline=True)
        embed.add_field(name="🔄 Sesiones",       value=str(sessions), inline=True)
        if p["country"]:
            flag = server_service.country_to_flag(p["country"])
            embed.add_field(name="🌍 País", value=f"{flag} {p['country']}".strip(), inline=True)
        if p["vac_banned"]:
            embed.add_field(name="⚠️ Atención", value="Cuenta con VAC ban registrado", inline=False)

        embed.set_footer(
            text=f"Vinculado a {interaction.user.display_name}",
            icon_url=interaction.user.display_avatar.url,
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

        s = server_service.build_server_state(state, slots)
        embed = discord.Embed(title="🖥️ Estado del Servidor", color=0x57F287)
        embed.add_field(name="🗺️ Mapa actual",     value=s["current_map"], inline=False)
        embed.add_field(name="⏭️ Próximo mapa",    value=s["next_map"],    inline=False)
        embed.add_field(name="👥 Jugadores",
                        value=f"{s['allied']+s['axis']}/{s['max_players']} (Aliados: {s['allied']} | Eje: {s['axis']})",
                        inline=False)
        embed.add_field(name="🏆 Score",
                        value=f"Aliados {s['score_allied']} — {s['score_axis']} Eje", inline=False)
        embed.add_field(name="⏱️ Tiempo restante", value=s["time_rem"], inline=False)
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
        embed  = discord.Embed(title=f"🟢 {len(players)} jugadores conectados", color=0x57F287)
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name="\u200b" if i > 0 else "Jugadores",
                value="\n".join(f"• {n}" for n in chunk),
                inline=True,
            )
        await interaction.followup.send(embed=embed)

    # ── /hll vip ──────────────────────────────────────────────
    @group.command(name="vip", description="Verificá si tenés VIP")
    @player_or_admin()
    async def vip(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with pool.acquire() as conn:
            steam_id = await db_guild.get_linked_steam_id_for_discord(conn, interaction.user.id)

        if not steam_id:
            await interaction.followup.send(
                "❌ Vinculá tu Steam ID primero con `/hll registro`.", ephemeral=True
            )
            return

        try:
            vips = await crcon.get_vip_ids()
        except CRCONError as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)
            return

        entry = next((v for v in (vips or []) if v.get("player_id") == steam_id), None)
        if entry:
            vence = server_service.format_vip_expiration(entry.get("vip_expiration"))
            await interaction.followup.send(
                f"⭐ **Tenés VIP activo**\n"
                f"Nombre VIP: {entry.get('name') or '—'}\n"
                f"Vence: {vence}",
                ephemeral=True,
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

        cantidad     = max(1, min(cantidad, 20))
        col          = categoria.value
        period_value = periodo.value if periodo else "all"

        rows = await fetch_leaderboard(pool, col, period_value, cantidad)
        if not rows:
            label = periodo.name if periodo else "Histórico"
            await interaction.followup.send(f"No hay datos para ese período ({label}).")
            return

        embed = build_leaderboard_embed(rows, col, categoria.name, period_value, cantidad)
        await interaction.followup.send(embed=embed)

    # ── Autocomplete arma ─────────────────────────────────────
    async def weapon_arma_autocomplete(interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [w for w in HLL_WEAPONS if current_lower in w.lower()]
        return [app_commands.Choice(name=w, value=w) for w in matches[:25]]

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
            color=0xE67E22,
        )
        embed.set_footer(text="📊 Stats históricos acumulados")
        await interaction.followup.send(embed=embed)


    # ── /hlladmin armas ───────────────────────────────────────
    @admin_group.command(name="armas", description="Lista todas las armas con kills registrados")
    async def armas(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with pool.acquire() as conn:
            rows = await db_matches.get_all_weapons_totals(conn)

        if not rows:
            await interaction.followup.send("No hay kills registrados todavía.", ephemeral=True)
            return

        content = "\n".join(f"{r['weapon']} ({r['total_kills']} kills)" for r in rows)
        file = discord.File(fp=io.BytesIO(content.encode()), filename="armas.txt")
        await interaction.followup.send(
            f"🔫 {len(rows)} armas registradas. Usá el nombre exacto en `kills_weapon:NOMBRE:10`.",
            file=file,
            ephemeral=True,
        )

    # ── /hlladmin snapshot ────────────────────────────────────
    @admin_group.command(name="snapshot", description="Manda el resumen Top 10 de un período")
    @app_commands.describe(
        periodo="Qué resumen mandar: día, semana o mes",
        fecha="Fecha de referencia DD/MM/AAAA (opcional, default: hoy)"
    )
    @app_commands.choices(periodo=[
        app_commands.Choice(name="Día",    value="day"),
        app_commands.Choice(name="Semana", value="week"),
        app_commands.Choice(name="Mes",    value="month"),
    ])
    @admin_only()
    async def snapshot(interaction: discord.Interaction,
                        periodo: app_commands.Choice[str] = None,
                        fecha: str = None):
        await interaction.response.defer(ephemeral=True)

        reference_date = None
        if fecha:
            try:
                reference_date = datetime.strptime(fecha.strip(), "%d/%m/%Y").replace(tzinfo=TZ_UY)
            except ValueError:
                await interaction.followup.send(
                    "❌ Formato inválido. Usá DD/MM/AAAA (ej: 20/06/2026).", ephemeral=True
                )
                return

        async with pool.acquire() as conn:
            channel_id = await db_guild.get_snapshot_channel(conn, interaction.guild_id)

        if not channel_id:
            await interaction.followup.send(
                "❌ No hay canal de snapshots configurado. Usá `/hlladmin setchannel`.",
                ephemeral=True,
            )
            return

        period_value = periodo.value if periodo else "day"
        await run_snapshot_manual(
            interaction.client, pool, interaction.guild_id, channel_id,
            period_value, reference_date
        )
        await interaction.followup.send(f"✅ Snapshot enviado a <#{channel_id}>.", ephemeral=True)

    # ── /hll help ─────────────────────────────────────────────
    def _build_player_help_embed() -> discord.Embed:
        embed = discord.Embed(title="📖 Comandos HLL Bot", color=0x5865F2)
        embed.add_field(name="/hll registro <steam_id>",          value="Vincula tu Discord con tu Steam ID", inline=False)
        embed.add_field(name="/hll perfil",                        value="Tu perfil en CRCON (sesiones, horas, VIP)", inline=False)
        embed.add_field(name="/hll server",                        value="Estado del servidor (mapa, jugadores, score)", inline=False)
        embed.add_field(name="/hll online",                        value="Jugadores conectados ahora mismo", inline=False)
        embed.add_field(name="/hll vip",                           value="Verificá si tenés VIP activo", inline=False)
        embed.add_field(name="/hll top <categoria> [periodo]",     value="Ranking: Kills, K/D, Partidas, etc.", inline=False)
        embed.add_field(name="/hll weapon <arma>",                 value="Top 10 de jugadores con más kills usando esa arma", inline=False)
        embed.add_field(name="/hll desafio listar",                value="Muestra los desafíos activos", inline=False)
        embed.add_field(name="/hll desafio progreso",              value="Ranking de progreso de un desafío (con autocomplete)", inline=False)
        embed.add_field(name="/stats show",                        value="Tus stats acumulados", inline=False)
        embed.add_field(name="/stats games [cantidad]",            value="Tus últimas N partidas", inline=False)
        embed.add_field(name="/stats weapon",                      value="Tus kills con tus armas + Ranking", inline=False)
        return embed

    @group.command(name="help", description="Lista de comandos disponibles")
    async def help_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(embed=_build_player_help_embed(), ephemeral=True)

    @admin_group.command(name="help", description="Lista de TODOS los comandos (jugador + admin)")
    @admin_only()
    async def admin_help_cmd(interaction: discord.Interaction):
        embed = _build_player_help_embed()
        embed.add_field(name="── Admin (/hlladmin) ──",             value="\u200b", inline=False)
        embed.add_field(name="/hlladmin snapshot [periodo]",        value="Manda ahora el resumen Top 10", inline=False)
        embed.add_field(name="/hlladmin setchannel #canal",         value="Configura los canales", inline=False)
        embed.add_field(name="/hlladmin setroles @admin @player",   value="Configura los roles", inline=False)
        embed.add_field(name="/hlladmin config",                    value="Muestra la configuración actual", inline=False)
        embed.add_field(name="/hlladmin armas",                     value="Manda un .txt con el nombre de las armas", inline=False)
        embed.add_field(name="/hlladmin desafio metricas",          value="Lista las métricas disponibles", inline=False)
        embed.add_field(name="/hlladmin desafio crear",             value="Crea un desafío configurable", inline=False)
        embed.add_field(name="/hlladmin desafio eliminar <id>",     value="Desactiva un desafío", inline=False)
        embed.add_field(name="/hlladmin desafio plantilla",         value="Descarga JSON de ejemplo para importar desafíos", inline=False)
        embed.add_field(name="/hlladmin desafio importar",          value="Crea desafíos en lote desde un archivo JSON", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    return group, admin_group