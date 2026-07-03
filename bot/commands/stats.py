"""
commands/stats.py
Grupo /stats: show, games, weapon
"""
import discord
from discord import app_commands
from discord.ext import commands

from checks import player_or_admin
from timeutils import format_local
from services import stats as stats_service
from services.leaderboard import get_all_weapons_with_rank


def setup_stats(bot: commands.Bot, pool):
    group = app_commands.Group(name="stats", description="Tus estadísticas en HLL")

    # ── /stats show ───────────────────────────────────────────
    @group.command(name="show", description="Tus stats acumulados históricos")
    @player_or_admin()
    async def show(interaction: discord.Interaction):
        await interaction.response.defer()

        steam_id, row, ranks, top_weapons = await stats_service.get_player_stats(
            pool, interaction.user.id
        )

        if not steam_id:
            await interaction.followup.send(
                "❌ Vinculá tu Steam ID primero con `/hll registro <steam_id>`."
            )
            return

        if not row:
            await interaction.followup.send(
                "No tenés partidas registradas todavía. El collector las procesa cada 30 minutos."
            )
            return

        total_h = round((row["total_time_seconds"] or 0) / 3600, 1)

        def rank_suffix(col: str) -> str:
            r = ranks.get(col)
            if not r:
                return ""
            rank, total = r
            return f" (#{rank} de {total})"

        stats_lines = [
            f"🎮 **Partidas:** {row['matches_played']}{rank_suffix('matches_played')}",
            f"💀 **Kills:** {row['total_kills']}{rank_suffix('total_kills')}",
            f"☠️ **Deaths:** {row['total_deaths']}",
            f"⚔️ **K/D:** {row['kd_ratio']}{rank_suffix('kd_ratio')}",
            f"🔥 **Combat:** {row['total_combat']}{rank_suffix('total_combat')}",
            f"⚔️ **Offense:** {row['total_offense']}{rank_suffix('total_offense')}",
            f"🛡️ **Defense:** {row['total_defense']}{rank_suffix('total_defense')}",
            f"🤝 **Support:** {row['total_support']}{rank_suffix('total_support')}",
            f"⏱️ **Horas:** {total_h}h{rank_suffix('total_time_seconds')}",
        ]

        embed = discord.Embed(
            title=f"📊 Stats de {row['last_name']}",
            description="\n".join(stats_lines),
            color=0x5865F2,
        )

        if top_weapons:
            weapons_str = "\n".join(
                f"`{i+1}.` {w['weapon']} — **{w['kills']}** kills"
                for i, w in enumerate(top_weapons)
            )
            embed.add_field(name="🔫 Top armas", value=weapons_str, inline=False)

        embed.set_footer(text=f"Steam ID: {steam_id}")
        await interaction.followup.send(embed=embed)

    # ── /stats games ──────────────────────────────────────────
    @group.command(name="games", description="Tus últimas partidas")
    @app_commands.describe(cantidad="Cuántas partidas mostrar (máx 10)")
    @player_or_admin()
    async def games(interaction: discord.Interaction, cantidad: int = 5):
        await interaction.response.defer()

        cantidad = max(1, min(cantidad, 10))
        steam_id, rows = await stats_service.get_player_recent_games(
            pool, interaction.user.id, cantidad
        )

        if not steam_id:
            await interaction.followup.send(
                "❌ Vinculá tu Steam ID primero con `/hll registro <steam_id>`."
            )
            return

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
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    # ── /stats weapon ─────────────────────────────────────────
    @group.command(name="weapon", description="Todas tus armas con kills y tu ranking en cada una")
    @player_or_admin()
    async def weapon(interaction: discord.Interaction):
        await interaction.response.defer()

        steam_id, weapons = await stats_service.get_player_weapons(
            pool, interaction.user.id
        )

        if not steam_id:
            await interaction.followup.send(
                "❌ Vinculá tu Steam ID primero con `/hll registro <steam_id>`."
            )
            return

        if not weapons:
            await interaction.followup.send(
                "No tenés kills registrados con ninguna arma todavía."
            )
            return

        lines = [
            f"`{w['weapon']}` — **{w['kills']}** kills · #{w['rank']} de {w['total_players']}"
            for w in weapons
        ]

        description = "\n".join(lines)
        if len(description) > 4000:
            shown, total_len = [], 0
            for line in lines:
                if total_len + len(line) + 1 > 3950:
                    break
                shown.append(line)
                total_len += len(line) + 1
            faltantes = len(lines) - len(shown)
            description = "\n".join(shown) + f"\n\n_... y {faltantes} arma(s) más_"

        embed = discord.Embed(
            title="🔫 Tus armas",
            description=description,
            color=0x5865F2,
        )
        embed.set_footer(text=f"{len(weapons)} arma(s) distintas usadas")
        await interaction.followup.send(embed=embed)

    bot.tree.add_command(group)