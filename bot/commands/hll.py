"""
commands/hll.py
Grupo /hll con subcomandos: registro, help, server, online, top, vip
"""
import discord
from discord import app_commands
from discord.ext import commands

from api import crcon, CRCONError


def setup_hll(bot: commands.Bot, pool):
    group = app_commands.Group(name="hll", description="Comandos de Hell Let Loose")

    # ── /hll registro <steam_id> ──────────────────────────────
    @group.command(name="registro", description="Vinculá tu cuenta de Discord con tu Steam ID")
    @app_commands.describe(steam_id="Tu Steam ID de 64 bits (ej: 76561198XXXXXXXXX)")
    async def registro(interaction: discord.Interaction, steam_id: str):
        await interaction.response.defer(ephemeral=True)

        # Validación básica
        if not steam_id.isdigit() or len(steam_id) != 17:
            await interaction.followup.send(
                "❌ Steam ID inválido. Debe tener 17 dígitos.\n"
                "Encontralo en: https://steamid.io", ephemeral=True
            )
            return

        async with pool.acquire() as conn:
            # Ver si ya está vinculado otro usuario con ese steam_id
            existing = await conn.fetchrow(
                "SELECT discord_id FROM linked_players WHERE steam_id = $1", steam_id
            )
            if existing and existing["discord_id"] != interaction.user.id:
                await interaction.followup.send(
                    "❌ Ese Steam ID ya está vinculado a otra cuenta de Discord.", ephemeral=True
                )
                return

            await conn.execute(
                """
                INSERT INTO linked_players (discord_id, steam_id, discord_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (discord_id) DO UPDATE
                  SET steam_id = $2, discord_name = $3
                """,
                interaction.user.id,
                steam_id,
                str(interaction.user),
            )

        await interaction.followup.send(
            f"✅ Vinculado correctamente.\n"
            f"Discord: **{interaction.user}**\n"
            f"Steam ID: `{steam_id}`", ephemeral=True
        )

    # ── /hll perfil ───────────────────────────────────────────
    @group.command(name="perfil", description="Muestra tu perfil en CRCON")
    async def perfil(interaction: discord.Interaction):
        await interaction.response.defer()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT steam_id FROM linked_players WHERE discord_id = $1",
                interaction.user.id
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

        names     = data.get("names", [{}])
        last_name = names[0].get("name", "?") if names else "?"
        sessions  = data.get("sessions_count", 0)
        total_h   = round(data.get("total_playtime_seconds", 0) / 3600, 1)
        flags     = " ".join(f.get("flag", "") for f in data.get("flags", [])) or "Ninguna"
        vips      = await crcon.get_vip_ids()
        is_vip    = any(v.get("player_id") == row["steam_id"] for v in (vips or []))

        embed = discord.Embed(title=f"👤 {last_name}", color=0x2f3136)
        embed.add_field(name="Steam ID",   value=f"`{row['steam_id']}`",  inline=True)
        embed.add_field(name="VIP",        value="⭐ Sí" if is_vip else "No", inline=True)
        embed.add_field(name="Sesiones",   value=str(sessions),           inline=True)
        embed.add_field(name="Horas totales", value=f"{total_h}h",        inline=True)
        embed.add_field(name="Flags",      value=flags,                   inline=True)
        embed.set_footer(text=f"Discord: {interaction.user}")
        await interaction.followup.send(embed=embed)

    # ── /hll server ───────────────────────────────────────────
    @group.command(name="server", description="Estado actual del servidor")
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
        time_rem     = state.get("time_remaining", "?")
        score_allied = state.get("allied_score", 0)
        score_axis   = state.get("axis_score", 0)
        max_players  = slots.get("max_players", 100) if slots else 100

        embed = discord.Embed(title="🖥️ Estado del Servidor", color=0x57F287)
        embed.add_field(name="🗺️ Mapa actual", value=current_map, inline=False)
        embed.add_field(name="⏭️ Próximo mapa", value=next_map,   inline=False)
        embed.add_field(name="👥 Jugadores",
                        value=f"{allied + axis}/{max_players} (Aliados: {allied} | Eje: {axis})",
                        inline=False)
        embed.add_field(name="🏆 Score",
                        value=f"Aliados {score_allied} — {score_axis} Eje",
                        inline=True)
        embed.add_field(name="⏱️ Tiempo restante", value=str(time_rem), inline=True)
        await interaction.followup.send(embed=embed)

    # ── /hll online ───────────────────────────────────────────
    @group.command(name="online", description="Jugadores conectados ahora")
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

        names = [p.get("name", "?") for p in players[:50]]
        chunks = [names[i:i+25] for i in range(0, len(names), 25)]

        embed = discord.Embed(
            title=f"🟢 {len(players)} jugadores conectados",
            color=0x57F287
        )
        for i, chunk in enumerate(chunks):
            embed.add_field(
                name="\u200b" if i > 0 else "Jugadores",
                value="\n".join(f"• {n}" for n in chunk),
                inline=True
            )
        await interaction.followup.send(embed=embed)

    # ── /hll vip ──────────────────────────────────────────────
    @group.command(name="vip", description="Verificá si tenés VIP")
    async def vip(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT steam_id FROM linked_players WHERE discord_id = $1",
                interaction.user.id
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
    @group.command(name="top", description="Ranking histórico de jugadores")
    @app_commands.describe(
        categoria="Qué querés rankear",
        cantidad="Cuántos jugadores mostrar (máx 20)"
    )
    @app_commands.choices(categoria=[
        app_commands.Choice(name="Kills",     value="total_kills"),
        app_commands.Choice(name="K/D",       value="kd_ratio"),
        app_commands.Choice(name="Partidas",  value="matches_played"),
        app_commands.Choice(name="Combat",    value="total_combat"),
        app_commands.Choice(name="Offense",   value="total_offense"),
        app_commands.Choice(name="Defense",   value="total_defense"),
        app_commands.Choice(name="Support",   value="total_support"),
    ])
    async def top(interaction: discord.Interaction,
                  categoria: app_commands.Choice[str],
                  cantidad: int = 10):
        await interaction.response.defer()

        cantidad = max(1, min(cantidad, 20))
        col      = categoria.value

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT last_name, {col}, matches_played, total_kills, total_deaths, kd_ratio
                FROM player_totals
                ORDER BY {col} DESC NULLS LAST
                LIMIT $1
                """,
                cantidad
            )

        if not rows:
            await interaction.followup.send("No hay datos todavía. El collector aún no procesó partidas.")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, r in enumerate(rows):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            value = r[col]
            if isinstance(value, float):
                value = f"{value:.2f}"
            lines.append(f"{medal} **{r['last_name']}** — {value}")

        embed = discord.Embed(
            title=f"🏆 Top {cantidad} — {categoria.name}",
            description="\n".join(lines),
            color=0xF1C40F
        )
        embed.set_footer(text="Stats históricos acumulados")
        await interaction.followup.send(embed=embed)

    # ── /hll help ─────────────────────────────────────────────
    @group.command(name="help", description="Lista de comandos disponibles")
    async def help_cmd(interaction: discord.Interaction):
        embed = discord.Embed(title="📖 Comandos HLL Bot", color=0x5865F2)
        embed.add_field(name="/hll registro <steam_id>",
                        value="Vincula tu Discord con tu Steam ID", inline=False)
        embed.add_field(name="/hll perfil",
                        value="Tu perfil en CRCON (sesiones, horas, flags, VIP)", inline=False)
        embed.add_field(name="/hll server",
                        value="Estado del servidor (mapa, jugadores, score)", inline=False)
        embed.add_field(name="/hll online",
                        value="Jugadores conectados ahora mismo", inline=False)
        embed.add_field(name="/hll vip",
                        value="Verificá si tenés VIP activo", inline=False)
        embed.add_field(name="/hll top <categoria> [cantidad]",
                        value="Ranking histórico: Kills, K/D, Partidas, Combat, etc.", inline=False)
        embed.add_field(name="/stats show",
                        value="Tus stats acumulados", inline=False)
        embed.add_field(name="/stats games [cantidad]",
                        value="Tus últimas N partidas", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.tree.add_command(group)
