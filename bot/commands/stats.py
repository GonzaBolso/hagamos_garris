"""
commands/stats.py
Grupo /stats con subcomandos: show, games
"""
import discord
from discord import app_commands
from discord.ext import commands

from checks import player_or_admin
from timeutils import format_local


def setup_stats(bot: commands.Bot, pool):
    group = app_commands.Group(name="stats", description="Tus estadísticas en HLL")

    # ── /stats show ───────────────────────────────────────────
    @group.command(name="show", description="Tus stats acumulados históricos")
    @player_or_admin()
    async def show(interaction: discord.Interaction):
        await interaction.response.defer()

        async with pool.acquire() as conn:
            link = await conn.fetchrow(
                "SELECT steam_id FROM linked_players WHERE discord_id = $1", interaction.user.id
            )
            if not link:
                await interaction.followup.send(
                    "❌ Vinculá tu Steam ID primero con `/hll registro <steam_id>`."
                )
                return

            row = await conn.fetchrow(
                "SELECT * FROM player_totals WHERE steam_id = $1", link["steam_id"]
            )

        if not row:
            await interaction.followup.send(
                "No tenés partidas registradas todavía. El collector las procesa cada 30 minutos."
            )
            return

        total_h = round((row["total_time_seconds"] or 0) / 3600, 1)

        embed = discord.Embed(title=f"📊 Stats de {row['last_name']}", color=0x5865F2)
        embed.add_field(name="🎮 Partidas",  value=str(row["matches_played"]), inline=True)
        embed.add_field(name="💀 Kills",     value=str(row["total_kills"]),    inline=True)
        embed.add_field(name="☠️ Deaths",    value=str(row["total_deaths"]),   inline=True)
        embed.add_field(name="⚔️ K/D",       value=str(row["kd_ratio"]),       inline=True)
        embed.add_field(name="🔥 Combat",    value=str(row["total_combat"]),   inline=True)
        embed.add_field(name="⚔️ Offense",   value=str(row["total_offense"]),  inline=True)
        embed.add_field(name="🛡️ Defense",   value=str(row["total_defense"]),  inline=True)
        embed.add_field(name="🤝 Support",   value=str(row["total_support"]),  inline=True)
        embed.add_field(name="⏱️ Horas",     value=f"{total_h}h",             inline=True)
        embed.set_footer(text=f"Steam ID: {link['steam_id']}")
        await interaction.followup.send(embed=embed)

    # ── /stats games ──────────────────────────────────────────
    @group.command(name="games", description="Tus últimas partidas")
    @app_commands.describe(cantidad="Cuántas partidas mostrar (máx 10)")
    @player_or_admin()
    async def games(interaction: discord.Interaction, cantidad: int = 5):
        await interaction.response.defer()

        cantidad = max(1, min(cantidad, 10))

        async with pool.acquire() as conn:
            link = await conn.fetchrow(
                "SELECT steam_id FROM linked_players WHERE discord_id = $1", interaction.user.id
            )
            if not link:
                await interaction.followup.send(
                    "❌ Vinculá tu Steam ID primero con `/hll registro <steam_id>`."
                )
                return

            rows = await conn.fetch(
                """
                SELECT
                    m.map_name, m.start_time, m.allied_score, m.axis_score,
                    mps.kills, mps.deaths, mps.combat_score,
                    mps.offense_score, mps.defense_score, mps.support_score, mps.time_seconds
                FROM match_player_stats mps
                JOIN matches m USING (match_id)
                WHERE mps.steam_id = $1
                ORDER BY m.start_time DESC NULLS LAST
                LIMIT $2
                """,
                link["steam_id"], cantidad
            )

        if not rows:
            await interaction.followup.send("No tenés partidas registradas todavía.")
            return

        embed = discord.Embed(title=f"🎮 Últimas {len(rows)} partidas", color=0x5865F2)
        for r in rows:
            fecha = format_local(r["start_time"], "%d/%m %H:%M")
            kd    = round(r["kills"] / r["deaths"], 2) if r["deaths"] else r["kills"]
            horas = round(r["time_seconds"] / 3600, 1) if r["time_seconds"] else 0
            score = f"{r['allied_score']}–{r['axis_score']}"

            embed.add_field(
                name=f"🗺️ {r['map_name'] or '?'} — {fecha}",
                value=(
                    f"Score: {score} | Tiempo: {horas}h\n"
                    f"K: **{r['kills']}** D: **{r['deaths']}** KD: **{kd}**\n"
                    f"Combat: {r['combat_score']} | Off: {r['offense_score']} "
                    f"| Def: {r['defense_score']} | Sup: {r['support_score']}"
                ),
                inline=False
            )

        await interaction.followup.send(embed=embed)

    bot.tree.add_command(group)