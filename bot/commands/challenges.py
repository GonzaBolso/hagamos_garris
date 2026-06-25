"""
commands/challenges.py
Subgrupo /hll desafio: listar, progreso (jugador)
Subgrupo /hlladmin desafio: metricas, crear, eliminar (admin)

Soporta:
  - Múltiples métricas por desafío (todas deben cumplirse — AND)
  - Períodos: diario, semanal, personalizado, partida actual, próxima partida
"""
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

from checks import admin_only, player_or_admin
from timeutils import format_local


METRIC_LABELS = {
    "kills":    "💀 Kills",
    "kd_ratio": "⚔️ K/D",
    "matches":  "🎮 Partidas",
    "combat":   "🔥 Combat",
    "offense":  "⚔️ Offense",
    "defense":  "🛡️ Defense",
    "support":  "🤝 Support",
}

VALID_METRICS = set(METRIC_LABELS.keys())

PERIOD_LABELS = {
    "daily":         "Diario",
    "weekly":        "Semanal",
    "custom":        "Personalizado",
    "current_match": "Partida actual",
    "next_match":    "Próxima partida",
}


def parse_metrics(metricas_str: str):
    """
    Parsea un string tipo 'kills:20,kd_ratio:2' en una lista de (metric, target).
    Lanza ValueError si el formato es inválido.
    """
    pairs = []
    for chunk in metricas_str.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.match(r"^([a-z_]+)\s*:\s*([\d.]+)$", chunk)
        if not match:
            raise ValueError(f"Formato inválido en '{chunk}'. Usá metrica:valor (ej: kills:20)")
        metric, target = match.group(1), float(match.group(2))
        if metric not in VALID_METRICS:
            opciones = ", ".join(VALID_METRICS)
            raise ValueError(f"Métrica desconocida '{metric}'. Opciones válidas: {opciones}")
        pairs.append((metric, target))
    if not pairs:
        raise ValueError("Tenés que especificar al menos una métrica.")
    return pairs


def format_metrics_line(metrics: list) -> str:
    """metrics: lista de dicts con 'metric' y 'target' (o tuplas)."""
    parts = []
    for m in metrics:
        metric = m["metric"] if isinstance(m, dict) else m[0]
        target = m["target"] if isinstance(m, dict) else m[1]
        label = METRIC_LABELS.get(metric, metric)
        parts.append(f"{label} ≥ {float(target):g}")
    return " **Y** ".join(parts)


def setup_challenges(hll_group: app_commands.Group, admin_group: app_commands.Group, pool):
    sub = app_commands.Group(name="desafio", description="Desafíos automáticos de stats", parent=hll_group)
    admin_sub = app_commands.Group(name="desafio", description="Administración de desafíos", parent=admin_group)

    # ── /hlladmin desafio metricas ─────────────────────────────
    @admin_sub.command(name="metricas", description="Lista las métricas disponibles para crear desafíos")
    async def metricas_cmd(interaction: discord.Interaction):
        lines = [f"`{key}` — {label}" for key, label in METRIC_LABELS.items()]
        embed = discord.Embed(
            title="📋 Métricas disponibles",
            description=(
                "\n".join(lines) +
                "\n\n**Formato:** `metrica:objetivo` separadas por coma\n"
                "Ejemplo: `kills:20,kd_ratio:2` (debe cumplir ambas)"
            ),
            color=0x5865F2
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /hlladmin desafio crear ───────────────────────────────
    @admin_sub.command(name="crear", description="Crea un desafío configurable")
    @app_commands.describe(
        nombre="Nombre del desafío (ej: 'Cazador de la semana')",
        metricas="kills, kd_ratio, matches, combat, offense, defense, support — ej: 'kills:20,kd_ratio:2'",
        periodo="Duración del desafío",
        dias_custom="Si elegís 'Personalizado', cuántos días dura"
    )
    @app_commands.choices(
        periodo=[
            app_commands.Choice(name="Diario",            value="daily"),
            app_commands.Choice(name="Semanal",           value="weekly"),
            app_commands.Choice(name="Personalizado",     value="custom"),
            app_commands.Choice(name="Partida actual",    value="current_match"),
            app_commands.Choice(name="Próxima partida",   value="next_match"),
        ],
    )
    @admin_only()
    async def crear(interaction: discord.Interaction,
                    nombre: str,
                    metricas: str,
                    periodo: app_commands.Choice[str],
                    dias_custom: int = 7):
        await interaction.response.defer(ephemeral=True)

        try:
            parsed_metrics = parse_metrics(metricas)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        match_id = None
        start_date = now
        end_date = None  # se completa después según el período

        if periodo.value == "daily":
            end_date = now + timedelta(days=1)
        elif periodo.value == "weekly":
            end_date = now + timedelta(days=7)
        elif periodo.value == "custom":
            dias_custom = max(1, min(dias_custom, 90))
            end_date = now + timedelta(days=dias_custom)
        elif periodo.value == "current_match":
            # Se resuelve contra la partida en curso; sin fecha de fin fija,
            # el collector la cierra cuando detecta que la partida terminó.
            start_date = None
            end_date = None
        elif periodo.value == "next_match":
            # Arranca a contar desde el próximo cambio de mapa.
            start_date = None
            end_date = None

        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO challenges
                        (guild_id, name, description, period, start_date, end_date, match_id, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    RETURNING id
                    """,
                    interaction.guild_id, nombre, None, periodo.value,
                    start_date, end_date, match_id, interaction.user.id
                )
                challenge_id = row["id"]

                for metric, target in parsed_metrics:
                    await conn.execute(
                        "INSERT INTO challenge_metrics (challenge_id, metric, target) VALUES ($1, $2, $3)",
                        challenge_id, metric, target
                    )

        metrics_line = format_metrics_line(
            [{"metric": m, "target": t} for m, t in parsed_metrics]
        )

        vence_txt = format_local(end_date) if end_date else f"({PERIOD_LABELS[periodo.value]})"

        await interaction.followup.send(
            f"✅ Desafío **#{challenge_id} — {nombre}** creado.\n"
            f"Condición: {metrics_line}\n"
            f"Período: {PERIOD_LABELS[periodo.value]} • Vence: {vence_txt}",
            ephemeral=True
        )

    # ── /hll desafio listar ──────────────────────────────────
    @sub.command(name="listar", description="Muestra los desafíos activos")
    @player_or_admin()
    async def listar(interaction: discord.Interaction):
        await interaction.response.defer()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM challenges
                WHERE guild_id = $1 AND active = TRUE
                  AND (end_date IS NULL OR end_date > NOW())
                ORDER BY id DESC
                """,
                interaction.guild_id
            )
            if not rows:
                await interaction.followup.send("📭 No hay desafíos activos en este momento.")
                return

            embed = discord.Embed(title="🎯 Desafíos Activos", color=0x5865F2)
            for r in rows:
                metrics = await conn.fetch(
                    "SELECT metric, target FROM challenge_metrics WHERE challenge_id = $1",
                    r["id"]
                )
                metrics_line = format_metrics_line(metrics)
                vence = format_local(r["end_date"], "%d/%m %H:%M") if r["end_date"] else PERIOD_LABELS.get(r["period"], r["period"])
                embed.add_field(
                    name=f"#{r['id']} — {r['name']}",
                    value=f"{metrics_line}\nVence: {vence}",
                    inline=False
                )

        await interaction.followup.send(embed=embed)

    # ── /hll desafio progreso ────────────────────────────────
    @sub.command(name="progreso", description="Mostrá el ranking de un desafío")
    @app_commands.describe(id="ID del desafío (usá /hll desafio listar para verlos)")
    @player_or_admin()
    async def progreso(interaction: discord.Interaction, id: int):
        await interaction.response.defer()

        async with pool.acquire() as conn:
            challenge = await conn.fetchrow(
                "SELECT * FROM challenges WHERE id = $1 AND guild_id = $2",
                id, interaction.guild_id
            )
            if not challenge:
                await interaction.followup.send("❌ No existe ese desafío.")
                return

            metrics = await conn.fetch(
                "SELECT id, metric, target FROM challenge_metrics WHERE challenge_id = $1",
                id
            )

            # Progreso consolidado (completed = TRUE solo si TODAS las métricas lo están)
            overall = await conn.fetch(
                """
                SELECT player_name, completed
                FROM challenge_progress
                WHERE challenge_id = $1
                ORDER BY completed DESC, player_name ASC
                LIMIT 15
                """,
                id
            )

            # Progreso detallado por métrica, para mostrar números reales
            per_metric = {}
            for m in metrics:
                rows = await conn.fetch(
                    """
                    SELECT steam_id, player_name, progress
                    FROM challenge_metric_progress
                    WHERE challenge_metric_id = $1
                    """,
                    m["id"]
                )
                per_metric[m["metric"]] = {r["steam_id"]: r["progress"] for r in rows}

        metrics_line = format_metrics_line(metrics)

        if not overall:
            await interaction.followup.send(
                f"📭 Todavía no hay progreso registrado para **{challenge['name']}**."
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(overall):
            medal = medals[i] if i < 3 else f"`{i+1}.`"
            check = " ✅" if r["completed"] else ""
            lines.append(f"{medal} **{r['player_name']}**{check}")

        completed_count = sum(1 for r in overall if r["completed"])

        embed = discord.Embed(
            title=f"🎯 #{id} — {challenge['name']}",
            description=f"Condición: {metrics_line}\n\n" + "\n".join(lines),
            color=0xF1C40F
        )
        embed.set_footer(text=f"{completed_count} jugador(es) completaron el desafío")
        await interaction.followup.send(embed=embed)

    # ── /hlladmin desafio eliminar ────────────────────────────
    @admin_sub.command(name="eliminar", description="Desactiva un desafío")
    @app_commands.describe(id="ID del desafío a eliminar")
    @admin_only()
    async def eliminar(interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE challenges SET active = FALSE WHERE id = $1 AND guild_id = $2",
                id, interaction.guild_id
            )

        if result == "UPDATE 0":
            await interaction.followup.send("❌ No existe ese desafío.", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Desafío #{id} desactivado.", ephemeral=True)