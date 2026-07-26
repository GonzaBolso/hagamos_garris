"""
commands/challenges.py
Subgrupo /hll desafio: listar, progreso (jugador)
Subgrupo /hlladmin desafio: metricas, crear, eliminar (admin)

Soporta:
  - Múltiples métricas por desafío (todas deben cumplirse — AND)
  - Períodos: diario, semanal, personalizado, partida actual, próxima partida
"""
import re
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

from checks import admin_only, player_or_admin
from timeutils import format_local
from services.leaderboard import TZ_UY, HLL_WEAPONS
from db import challenges as db_challenges

log = logging.getLogger(__name__)


METRIC_LABELS = {
    "kills":         "💀 Kills",
    "kd_ratio":      "⚔️ K/D",
    "matches":       "🎮 Partidas",
    "combat":        "🔥 Combat",
    "offense":       "⚔️ Offense",
    "defense":       "🛡️ Defense",
    "support":       "🤝 Support",
    "kills_weapon":  "🔫 Kills con arma",
    "kills_player":  "🎯 Kills a jugador",
    "kills_type":    "⚔️ Kills por tipo",
    "vehicles_destroyed": "🚗 Vehículos destruidos",
}

METRIC_EMOJIS = {
    "kills":         "💀",
    "kd_ratio":      "⚔️",
    "matches":       "🎮",
    "combat":        "🔥",
    "offense":       "⚔️",
    "defense":       "🛡️",
    "support":       "🤝",
    "kills_weapon":  "🔫",
    "kills_player":  "🎯",
    "kills_type":    "⚔️",
}

# Métricas que necesitan un parámetro extra (arma exacta o steam_id de
# víctima) además del target numérico. Formato: metrica:param:target
PARAM_METRICS = {"kills_weapon", "kills_player", "kills_type"}

VALID_METRICS = set(METRIC_LABELS.keys())

PERIOD_LABELS = {
    "custom":        "Personalizado",
    "current_match": "Partida actual",
}


def parse_metrics(metricas_str: str):
    """
    Parsea un string de métricas separadas por coma. Dos formatos:
      - 'metrica:target'           (ej: kills:20, kd_ratio:2)
      - 'metrica:param:target'     (ej: kills_weapon:BAZOOKA:10,
                                          kills_player:76561198XXXXXXXXX:5)
    El segundo formato es obligatorio para kills_weapon/kills_player
    (PARAM_METRICS), y no se acepta para el resto.
    Devuelve una lista de (metric, param_or_None, target).
    Lanza ValueError si el formato es inválido.
    """
    pairs = []
    for chunk in metricas_str.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue

        parts = chunk.split(":")

        if len(parts) == 2:
            metric, target_str = parts[0].strip(), parts[1].strip()
            param = None
        elif len(parts) == 3:
            metric, param, target_str = parts[0].strip(), parts[1].strip(), parts[2].strip()
        else:
            raise ValueError(
                f"Formato inválido en '{chunk}'. Usá metrica:valor "
                f"(ej: kills:20) o metrica:parametro:valor (ej: kills_weapon:BAZOOKA:10)"
            )

        if metric not in VALID_METRICS:
            opciones = ", ".join(VALID_METRICS)
            raise ValueError(f"Métrica desconocida '{metric}'. Opciones válidas: {opciones}")

        if metric in PARAM_METRICS and not param:
            raise ValueError(
                f"'{metric}' necesita un parámetro (arma o steam_id). "
                f"Formato: {metric}:parametro:valor"
            )
        if metric not in PARAM_METRICS and param is not None:
            raise ValueError(f"'{metric}' no acepta parámetro extra. Formato: {metric}:valor")

        if not re.match(r"^[\d.]+$", target_str):
            raise ValueError(f"Formato inválido en '{chunk}': el valor objetivo debe ser numérico")

        pairs.append((metric, param, float(target_str)))

    if not pairs:
        raise ValueError("Tenés que especificar al menos una métrica.")
    return pairs


async def format_metrics_line(pool, metrics: list) -> str:
    """
    metrics: lista de dicts o asyncpg.Record, cada uno con las claves
    'metric', 'target' y opcionalmente 'param'. Ambos tipos soportan
    acceso por nombre (m["metric"]), así que accedemos siempre así — NO
    por índice posicional, porque un asyncpg.Record con columnas extra
    (ej. SELECT id, metric, target, param) tiene posiciones distintas a
    un dict armado a mano con solo esas claves.

    Para 'kills_player', el param es un steam_id — se resuelve a nombre
    consultando la tabla players, en vez de mostrar el id crudo.
    """
    # Steam IDs a resolver de una vez, para no consultar la base por cada uno
    steam_ids_to_resolve = [
        m["param"] for m in metrics if m["metric"] == "kills_player" and m["param"]
    ]
    names_by_steam_id = {}
    if steam_ids_to_resolve:
        async with pool.acquire() as conn:
            names_by_steam_id = await db_challenges.resolve_player_names(conn, steam_ids_to_resolve)

    parts = []
    for m in metrics:
        metric = m["metric"]
        target = m["target"]
        param = m["param"]
        label = METRIC_LABELS.get(metric, metric)
        if param and metric == "kills_player":
            display_param = names_by_steam_id.get(param) or param
            parts.append(f"{label} ({display_param}) ≥ {float(target):g}")
        elif param:
            parts.append(f"{label} ({param}) ≥ {float(target):g}")
        else:
            parts.append(f"{label} ≥ {float(target):g}")
    return "\n".join(parts)


def format_vence(r) -> str:
    """
    Devuelve el texto a mostrar para 'Vence'/'Partida' de un desafío, según
    su período y estado actual. r: fila de challenges (dict o asyncpg.Record).
    """
    if r["end_date"]:
        return format_local(r["end_date"], "%d/%m %H:%M")

    if r["period"] == "current_match":
        map_name = r["map_name"]
        map_start = r["map_start"]
        if map_name and map_start:
            hora = datetime.fromtimestamp(map_start, tz=TZ_UY).strftime("%d/%m %H:%M")
            return f"{map_name} (inició {hora})"
        return "⏳ Esperando que arranque la próxima partida"

    return PERIOD_LABELS.get(r["period"], r["period"])


async def build_progress_embed(pool, challenge_id: int, guild_id: int):
    """
    Construye el embed de progreso de un desafío (mismo formato que usa
    /hll desafio progreso). Devuelve (embed, challenge) o (None, None) si
    el desafío no existe o todavía no hay progreso registrado.
    Reusada también por la tarea de notificación de cierre (challenge_close_task).
    """
    async with pool.acquire() as conn:
        challenge = await conn.fetchrow(
            "SELECT * FROM challenges WHERE id = $1 AND guild_id = $2",
            challenge_id, guild_id
        )
        if not challenge:
            return None, None

        metrics = await conn.fetch(
            "SELECT id, metric, target, param FROM challenge_metrics WHERE challenge_id = $1",
            challenge_id
        )

        overall = await conn.fetch(
            """
            SELECT steam_id, player_name, completed
            FROM challenge_progress
            WHERE challenge_id = $1
            """,
            challenge_id
        )

        # Clave por ID (no por metric) para soportar dos kills_weapon distintos
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
            per_metric[m["id"]] = {r["steam_id"]: r["progress"] for r in rows}

    metrics_line = await format_metrics_line(pool, metrics)

    if not overall:
        return None, challenge

    metric_ids    = [mr["id"]     for mr in metrics]
    metric_names  = [mr["metric"] for mr in metrics]
    total_metrics = len(metric_ids)
    min_ceros_para_ocultar = -(-total_metrics // 2)
    primary_id = metric_ids[0] if metric_ids else None

    visibles = []
    for r in overall:
        valores = [per_metric.get(mid, {}).get(r["steam_id"], 0) or 0 for mid in metric_ids]
        ceros = sum(1 for v in valores if v == 0)
        if ceros >= min_ceros_para_ocultar:
            continue
        primary_value = per_metric.get(primary_id, {}).get(r["steam_id"], 0) or 0
        visibles.append((primary_value, r))

    visibles.sort(key=lambda t: t[0], reverse=True)
    visibles = visibles[:10]

    if not visibles:
        return None, challenge

    lines = []
    for i, (_, r) in enumerate(visibles):
        check = " ✅" if r["completed"] else ""
        valores_str = " ".join(
            f"{METRIC_EMOJIS.get(metric_names[j], '')}{per_metric.get(metric_ids[j], {}).get(r['steam_id'], 0):g}"
            for j in range(len(metric_ids))
        )
        lines.append(f"`{i+1}.` **{r['player_name']}** {valores_str}{check}")

    completed_count = sum(1 for r in overall if r["completed"])
    vence_info = format_vence(challenge)

    embed = discord.Embed(
        title=f"🎯 #{challenge_id} — {challenge['name']}",
        description=f"Condición:\n {metrics_line}\nPartida: {vence_info}\n\n" + "\n".join(lines),
        color=0xF1C40F
    )
    embed.set_footer(text=f"{completed_count} jugador(es) completaron el desafío")
    return embed, challenge


def setup_challenges(hll_group: app_commands.Group, admin_group: app_commands.Group, pool, crcon_client):
    sub = app_commands.Group(name="desafio", description="Desafíos automáticos de stats", parent=hll_group)
    admin_sub = app_commands.Group(name="desafio", description="Administración de desafíos", parent=admin_group)

    # ── /hlladmin desafio metricas ─────────────────────────────
    @admin_sub.command(name="metricas", description="Lista las métricas disponibles para crear desafíos")
    @admin_only()
    async def metricas_cmd(interaction: discord.Interaction):
        lines = [f"`{key}` — {label}" for key, label in METRIC_LABELS.items()]
        embed = discord.Embed(
            title="📋 Métricas disponibles",
            description=(
                "\n".join(lines) +
                "\n\n**Formato:** `metrica:objetivo` separadas por coma\n"
                "Ejemplo: `kills:20,kd_ratio:2` (debe cumplir ambas)\n\n"
                "**Con parámetro** (kills_weapon, kills_player): `metrica:parametro:objetivo`\n"
                "Ejemplo: `kills_weapon:BAZOOKA:10` (10 kills con BAZOOKA, nombre exacto del arma)\n"
                "Ejemplo: `kills_player:76561198XXXXXXXXX:5` (5 kills a ese steam_id)\n"
                "Ejemplo: `kills_type:infantry:50` (50 kills de tipo infantry)\n"
                "Tipos válidos: infantry, armor, machine_gun, sniper, bazooka, grenade, mine, satchel, commander, artillery, self_propelled_artillery"
            ),
            color=0x5865F2
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Autocompletado: arma (lista estática) ──────────────────
    async def arma_autocomplete(interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [w for w in HLL_WEAPONS if current_lower in w.lower()]
        return [
            app_commands.Choice(name=w, value=w)
            for w in matches[:25]  # Discord permite máximo 25 opciones
        ]

    # ── Autocompletado: jugador víctima (busca en la tabla players) ──
    async def jugador_victima_autocomplete(interaction: discord.Interaction, current: str):
        if not current:
            return []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT steam_id, player_name FROM players
                WHERE player_name ILIKE $1
                ORDER BY player_name
                LIMIT 25
                """,
                f"%{current}%"
            )
        return [
            app_commands.Choice(name=r["player_name"] or r["steam_id"], value=r["steam_id"])
            for r in rows
        ]

    # ── /hlladmin desafio crear ───────────────────────────────
    @admin_sub.command(name="crear", description="Crea un desafío configurable")
    @app_commands.describe(
        nombre="Nombre del desafío (ej: 'Cazador de la semana')",
        metricas="kills, kd_ratio, matches, combat, offense, defense, support, kills_weapon, kills_player, kills_type — ej: 'kills:20' o 'kills_weapon:$ARMA:10'",
        periodo="Duración del desafío",
        fecha_fin="Si elegís 'Personalizado': cuándo termina, formato DD/MM/AAAA HH:MM:SS (ej: 01/07/2026 22:00:00)",
        fecha_inicio="Opcional, solo 'Personalizado': desde cuándo cuenta. Mismo formato que fecha_fin",
        arma="Arma exacta para usar como $ARMA en metricas (ej: kills_weapon:$ARMA:10)",
        jugador_victima="Jugador para usar como $JUGADOR en metricas (ej: kills_player:$JUGADOR:5)"
    )
    @app_commands.autocomplete(arma=arma_autocomplete, jugador_victima=jugador_victima_autocomplete)
    @app_commands.choices(
        periodo=[
            app_commands.Choice(name="Personalizado",     value="custom"),
            app_commands.Choice(name="Partida actual",    value="current_match"),
        ],
    )
    @admin_only()
    async def crear(interaction: discord.Interaction,
                    nombre: str,
                    metricas: str,
                    periodo: app_commands.Choice[str],
                    fecha_fin: str = None,
                    fecha_inicio: str = None,
                    arma: str = None,
                    jugador_victima: str = None):
        await interaction.response.defer(ephemeral=True)

        # Reemplaza los placeholders $ARMA / $JUGADOR dentro de metricas
        # por los valores elegidos vía autocompletado, antes de parsear.
        if "$ARMA" in metricas:
            if not arma:
                await interaction.followup.send(
                    "❌ Usaste `$ARMA` en `metricas` pero no elegiste el parámetro `arma`.",
                    ephemeral=True
                )
                return
            metricas = metricas.replace("$ARMA", arma)
        if "$JUGADOR" in metricas:
            if not jugador_victima:
                await interaction.followup.send(
                    "❌ Usaste `$JUGADOR` en `metricas` pero no elegiste el parámetro `jugador_victima`.",
                    ephemeral=True
                )
                return
            metricas = metricas.replace("$JUGADOR", jugador_victima)

        try:
            parsed_metrics = parse_metrics(metricas)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        match_id = None
        start_date = now
        end_date = None  # se completa después según el período

        if periodo.value == "custom":
            if fecha_inicio:
                try:
                    parsed_inicio = datetime.strptime(fecha_inicio.strip(), "%d/%m/%Y %H:%M:%S")
                except ValueError:
                    await interaction.followup.send(
                        "❌ Formato de `fecha_inicio` inválido. Usá DD/MM/AAAA HH:MM:SS "
                        "(ej: 01/07/2026 20:00:00).",
                        ephemeral=True
                    )
                    return

                start_date = parsed_inicio.replace(tzinfo=TZ_UY).astimezone(timezone.utc)
                if start_date <= now:
                    await interaction.followup.send(
                        "❌ `fecha_inicio` tiene que ser una fecha/hora futura. Si querés que "
                        "el desafío arranque ahora, simplemente no pases este parámetro.",
                        ephemeral=True
                    )
                    return

            if not fecha_fin:
                await interaction.followup.send(
                    "❌ Para período Personalizado tenés que pasar `fecha_fin` "
                    "(formato DD/MM/AAAA HH:MM:SS, ej: 01/07/2026 22:00:00).",
                    ephemeral=True
                )
                return
            try:
                parsed_fin = datetime.strptime(fecha_fin.strip(), "%d/%m/%Y %H:%M:%S")
            except ValueError:
                await interaction.followup.send(
                    "❌ Formato de `fecha_fin` inválido. Usá DD/MM/AAAA HH:MM:SS "
                    "(ej: 01/07/2026 22:00:00).",
                    ephemeral=True
                )
                return

            end_date = parsed_fin.replace(tzinfo=TZ_UY).astimezone(timezone.utc)
            if end_date <= now:
                await interaction.followup.send(
                    "❌ `fecha_fin` tiene que ser una fecha/hora futura.",
                    ephemeral=True
                )
                return
            if end_date <= start_date:
                await interaction.followup.send(
                    "❌ `fecha_fin` tiene que ser posterior a `fecha_inicio`.",
                    ephemeral=True
                )
                return
        elif periodo.value == "current_match":
            # Se ancla directamente a la partida que está jugándose AHORA
            # (no a la próxima): consultamos el mapa en curso y lo guardamos
            # como map_name/map_start de una. El progreso en vivo de esa
            # partida lo calcula el live_polling del collector mientras
            # sigue en curso; cuando cierra, resolve_match_scope la
            # encuentra en 'matches' por start_time y resuelve match_id.
            start_date = now
            end_date = None
            try:
                info = await crcon_client.get_public_info()
                current_map = (info or {}).get("current_map") or {}
                map_start = current_map.get("start")
                map_name = (current_map.get("map") or {}).get("pretty_name") \
                    or ((current_map.get("map") or {}).get("map") or {}).get("pretty_name") or "?"
            except Exception as e:
                await interaction.followup.send(
                    f"❌ No pude consultar el estado del servidor para crear este desafío: {e}",
                    ephemeral=True
                )
                return

            if map_start is None:
                await interaction.followup.send(
                    "❌ No hay ninguna partida en curso en este momento según el servidor. "
                    "Probá de nuevo cuando haya un mapa corriendo.",
                    ephemeral=True
                )
                return

        async with pool.acquire() as conn:
            async with conn.transaction():
                extra = {"map_name": map_name, "map_start": map_start} if periodo.value == "current_match" else {}
                challenge_id = await db_challenges.create_challenge(
                    conn, interaction.guild_id, nombre, periodo.value,
                    start_date, end_date, match_id, interaction.user.id, **extra
                )
                for metric, param, target in parsed_metrics:
                    await db_challenges.add_challenge_metric(conn, challenge_id, metric, target, param)

        metrics_line = await format_metrics_line(
            pool, [{"metric": m, "param": p, "target": t} for m, p, t in parsed_metrics]
        )

        if periodo.value == "current_match":
            hora_inicio = datetime.fromtimestamp(map_start, tz=TZ_UY).strftime("%d/%m %H:%M")
            vence_txt = f"{map_name} (inició {hora_inicio})"
            comienza_txt = ""
        else:
            vence_txt = format_local(end_date) if end_date else f"({PERIOD_LABELS[periodo.value]})"
            comienza_txt = (
                f"Comienza: {format_local(start_date)}\n" if fecha_inicio else ""
            )

        await interaction.followup.send(
            f"✅ Desafío **#{challenge_id} — {nombre}** creado.",
            ephemeral=True
        )

        # Notificar al canal de desafíos si está configurado
        async with pool.acquire() as conn:
            channel_id = await db_challenges.get_guild_challenge_channel(conn, interaction.guild_id)
        if channel_id:
            try:
                channel = interaction.client.get_channel(channel_id) or                           await interaction.client.fetch_channel(channel_id)

                # Timestamp hammertime para la fecha de vencimiento
                if end_date:
                    ts = int(end_date.timestamp())
                    vence_hammertime = f"<t:{ts}:F> (<t:{ts}:R>)"
                elif periodo.value == "current_match":
                    vence_hammertime = f"Partida actual — {vence_txt}"
                else:
                    vence_hammertime = vence_txt

                metric_bullets = "\n".join(
                    f"• {line}" for line in metrics_line.split("\n")
                )

                embed = discord.Embed(
                    title=f"🎯 Nuevo desafío — {nombre}",
                    color=0x5865F2
                )
                embed.add_field(name="Condición", value=metric_bullets, inline=False)
                embed.add_field(name="Vence", value=vence_hammertime, inline=False)
                embed.add_field(name="📊 Progreso", value=f"`/hll desafio progreso`", inline=False)
                embed.set_footer(text=f"#{challenge_id} • {PERIOD_LABELS[periodo.value]}")

                await channel.send(embed=embed)

                # Mensaje in-game a todos los vinculados
                try:
                    async with pool.acquire() as pconn:
                        vinculados = await pconn.fetch(
                            "SELECT steam_id FROM linked_players"
                        )
                    if not vinculados:
                        # Fallback: solo el creador
                        vinculados = []
                        async with pool.acquire() as pconn:
                            linked = await pconn.fetchrow(
                                "SELECT steam_id FROM linked_players WHERE discord_id = $1",
                                int(interaction.user.id)
                            )
                        if linked:
                            vinculados = [linked]

                    # Construir métricas con salto de línea y sin emojis
                    import re as _re
                    _emoji_re = _re.compile(
                        "[\U00010000-\U0010ffff"
                        "\U0001F300-\U0001F9FF"
                        "\u2600-\u27BF"
                        "\uFE00-\uFE0F]+",
                        flags=_re.UNICODE
                    )
                    metricas_lines = [
                        _emoji_re.sub("", line).strip()
                        for line in metrics_line.split("\n") if line.strip()
                    ]
                    msg_ingame = (
                        f"[Desafio #{challenge_id}] {nombre}\n"
                        + "\n".join(f"  {l}" for l in metricas_lines) + "\n"
                        f"Vence: {vence_txt}\n"
                        f"Ver ranking: /hll desafio progreso"
                    )
                    for v in vinculados:
                        try:
                            await crcon_client.message_player(
                                player_id=v["steam_id"],
                                player_name="",
                                message=msg_ingame,
                            )
                        except Exception:
                            pass
                except Exception as e:
                    log.warning(f"No se pudo mandar mensaje in-game: {e}")
            except Exception as e:
                log.warning(f"No se pudo notificar el desafío al canal: {e}")

    # ── /hll desafio listar ──────────────────────────────────
    @sub.command(name="listar", description="Muestra los desafíos activos")
    @player_or_admin()
    async def listar(interaction: discord.Interaction):
        await interaction.response.defer()

        async with pool.acquire() as conn:
            rows = await db_challenges.get_active_challenges(conn, interaction.guild_id)

        if not rows:
            await interaction.followup.send("📭 No hay desafíos activos en este momento.")
            return

        embed = discord.Embed(title="🎯 Desafíos Activos", color=0x5865F2)
        async with pool.acquire() as conn:
            for r in rows:
                metrics = await db_challenges.get_challenge_metrics(conn, r["id"])
                metrics_line = await format_metrics_line(pool, metrics)
                vence = format_vence(r)
                embed.add_field(
                    name=f"#{r['id']} — {r['name']}",
                    value=f"{metrics_line}\nVence: {vence}",
                    inline=False
                )

        await interaction.followup.send(embed=embed)

    # ── /hll desafio progreso ────────────────────────────────
    async def progreso_autocomplete(interaction: discord.Interaction, current: str):
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name FROM challenges
                WHERE guild_id = $1 AND active = TRUE
                  AND (end_date IS NULL OR end_date > NOW())
                ORDER BY id DESC
                LIMIT 25
                """,
                interaction.guild_id
            )
        return [
            app_commands.Choice(name=f"#{r['id']} — {r['name']}", value=r['id'])
            for r in rows
            if current.lower() in r['name'].lower() or current == ""
        ]

    @sub.command(name="progreso", description="Mostrá el ranking de un desafío")
    @app_commands.describe(id="Elegí el desafío")
    @app_commands.autocomplete(id=progreso_autocomplete)
    @player_or_admin()
    async def progreso(interaction: discord.Interaction, id: int):
        await interaction.response.defer()

        embed, challenge = await build_progress_embed(pool, id, interaction.guild_id)

        if challenge is None:
            await interaction.followup.send("❌ No existe ese desafío.")
            return

        if embed is None:
            await interaction.followup.send(
                f"📭 Todavía no hay progreso significativo registrado para **{challenge['name']}**."
            )
            return

        await interaction.followup.send(embed=embed)

    # ── /hlladmin desafio eliminar ────────────────────────────
    @admin_sub.command(name="eliminar", description="Desactiva un desafío")
    @app_commands.describe(id="ID del desafío a eliminar")
    @admin_only()
    async def eliminar(interaction: discord.Interaction, id: int):
        await interaction.response.defer(ephemeral=True)

        async with pool.acquire() as conn:
            updated = await db_challenges.deactivate_challenge(conn, id, interaction.guild_id)

        if not updated:
            await interaction.followup.send("❌ No existe ese desafío.", ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Desafío #{id} desactivado.", ephemeral=True)
    # ── /hlladmin desafio plantilla ───────────────────────────
    @admin_sub.command(name="plantilla", description="Descarga un JSON de ejemplo para importar desafíos")
    @admin_only()
    async def plantilla(interaction: discord.Interaction):
        import io, json as _json

        ejemplo = [
            {
                "nombre": "Semana de Rifles",
                "periodo": "semanal",
                "premio_vip_dias": 7,
                "excluir_roles": ["armycommander", "artillery", "tankcommander", "crewman", "spotter"],
                "metricas": [
                    {"tipo": "kills_weapon", "arma": "M1 GARAND", "objetivo": 100},
                    {"tipo": "kills", "objetivo": 200}
                ]
            },
            {
                "nombre": "Rifles de cada equipo",
                "periodo": "current_match",
                "premio_vip_dias": 3,
                "excluir_roles": ["armycommander", "artillery", "tankcommander", "crewman", "spotter"],
                "metricas": [
                    {
                        "tipo": "kills_weapon",
                        "armas": ["M1 GARAND", "GEWEHR 43", "SVT 40"],
                        "objetivo": 30,
                        "comentario": "armas equivalentes de aliados/eje — se suman"
                    },
                    {
                        "tipo": "kills_weapon",
                        "armas": ["MP40", "M1A1 THOMPSON", "PPSh-41"],
                        "objetivo": 20
                    }
                ]
            },
            {
                "nombre": "Partida perfecta",
                "periodo": "current_match",
                "metricas": [
                    {"tipo": "kills", "objetivo": 30},
                    {"tipo": "combat", "objetivo": 500}
                ]
            },
            {
                "nombre": "Desafío personalizado",
                "periodo": "custom",
                "inicio": "2026-07-05T00:00:00",
                "fin": "2026-07-12T23:59:59",
                "metricas": [
                    {"tipo": "kills_type", "tipo_kill": "armor", "objetivo": 20},
                    {"tipo": "kills_player", "steam_id": "76561199052198013", "objetivo": 5}
                ]
            }
        ]

        notas = {
            "_notas": {
                "periodos_validos": ["diario", "semanal", "mensual", "current_match", "next_match", "custom"],
                "metricas_validas": ["kills", "deaths", "kd_ratio", "matches", "combat", "offense", "defense", "support", "vehicles_destroyed", "kills_weapon", "kills_player", "kills_type"],
                "tipos_kill_validos": ["infantry", "armor", "machine_gun", "sniper", "bazooka", "grenade", "mine", "satchel", "commander", "artillery", "self_propelled_artillery"],
                "roles_validos": ["officer", "rifleman", "assault", "automaticrifleman", "medic", "support", "heavymachinegunner", "antitank", "engineer", "tankcommander", "crewman", "sniper", "spotter", "armycommander", "artillery"],
                "excluir_roles": "opcional — lista de roles a ignorar en el desafío",
                "premio_vip_dias": "opcional — días de VIP a dar a los que completen (0 = sin premio, se extiende si ya tienen VIP)",
                "armas": "opcional — lista de armas cuyas kills se suman (en vez de 'arma' para una sola)",
                "custom_requiere": "inicio y fin en formato ISO (YYYY-MM-DDTHH:MM:SS)",
                "kills_weapon_requiere": "campo 'arma' (una sola) o 'armas' (lista que se suma) con nombre exacto — usar /hlladmin armas",
                "kills_player_requiere": "campo 'steam_id' de la víctima",
                "kills_type_requiere": "campo 'tipo_kill'"
            },
            "desafios": ejemplo
        }

        data = _json.dumps(notas, ensure_ascii=False, indent=2)
        file = discord.File(fp=io.BytesIO(data.encode()), filename="plantilla_desafios.json")
        await interaction.response.send_message(
            "📄 Editá este archivo y subilo con `/hlladmin desafio importar`.",
            file=file,
            ephemeral=True
        )

    # ── /hlladmin desafio importar ───────────────────────────
    @admin_sub.command(name="importar", description="Crea desafíos desde un archivo JSON")
    @app_commands.describe(archivo="Archivo JSON con los desafíos a crear")
    @admin_only()
    async def importar(interaction: discord.Interaction, archivo: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        import io, json as _json

        if not archivo.filename.endswith(".json"):
            await interaction.followup.send("❌ El archivo debe ser un `.json`.", ephemeral=True)
            return

        try:
            raw = await archivo.read()
            data = _json.loads(raw.decode("utf-8"))
            desafios = data.get("desafios", data) if isinstance(data, dict) else data
            if not isinstance(desafios, list):
                raise ValueError("El JSON debe ser una lista de desafíos o un objeto con clave 'desafios'.")
        except Exception as e:
            await interaction.followup.send(f"❌ Error leyendo el archivo: `{e}`", ephemeral=True)
            return

        PERIODO_MAP = {
            "diario": "daily", "semanal": "weekly", "mensual": "monthly",
            "current_match": "current_match", "next_match": "next_match", "custom": "custom"
        }

        creados = []
        errores = []

        for i, d in enumerate(desafios):
            try:
                nombre  = d.get("nombre") or f"Desafío {i+1}"
                periodo = PERIODO_MAP.get(d.get("periodo", ""), d.get("periodo", ""))
                inicio  = d.get("inicio")
                fin     = d.get("fin")
                metricas = d.get("metricas") or []
                premio_vip_dias = int(d.get("premio_vip_dias", 0) or 0)

                if not metricas:
                    raise ValueError("Sin métricas")

                # Calcular fechas
                start_date = end_date = match_id = map_name = map_start = None
                if periodo == "current_match":
                    from datetime import datetime, timezone as _tz
                    start_date = datetime.now(_tz.utc)
                    end_date = None
                    try:
                        info = await crcon_client.get_public_info()
                        current_map = (info or {}).get("current_map") or {}
                        map_start = current_map.get("start")
                        map_name = (current_map.get("map") or {}).get("pretty_name")                             or ((current_map.get("map") or {}).get("map") or {}).get("pretty_name") or "?"
                    except Exception as e:
                        raise ValueError(f"No se pudo consultar el servidor: {e}")
                    if map_start is None:
                        raise ValueError("No hay partida en curso en este momento")
                elif periodo == "daily":
                    now = datetime.now(timezone.utc)
                    start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    end_date   = start_date + timedelta(days=1)
                elif periodo == "weekly":
                    now = datetime.now(timezone.utc)
                    start_date = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
                    end_date   = start_date + timedelta(weeks=1)
                elif periodo == "monthly":
                    now = datetime.now(timezone.utc)
                    start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    end_date   = (start_date + timedelta(days=32)).replace(day=1)
                elif periodo == "custom":
                    # El JSON manda hora local (UTC-3) sin offset — igual que parse_dt
                    from datetime import timedelta as _td
                    _LOCAL = timezone(_td(hours=-3))
                    start_date = datetime.fromisoformat(inicio).replace(tzinfo=_LOCAL).astimezone(timezone.utc) if inicio else None
                    end_date   = datetime.fromisoformat(fin).replace(tzinfo=_LOCAL).astimezone(timezone.utc) if fin else None

                parsed_metrics = []
                for m in metricas:
                    tipo = m.get("tipo")
                    obj  = float(m.get("objetivo", 0))
                    if tipo == "kills_weapon":
                        # Soportar "arma" (una) o "armas" (lista que se suma con |)
                        armas = m.get("armas")
                        if armas and isinstance(armas, list):
                            param = "|".join(a.strip() for a in armas if a.strip())
                        else:
                            param = m.get("arma")
                    elif tipo == "kills_player":
                        param = m.get("steam_id")
                    elif tipo == "kills_type":
                        param = m.get("tipo_kill")
                    else:
                        param = None
                    parsed_metrics.append((tipo, param, obj))

                async with pool.acquire() as conn:
                    async with conn.transaction():
                        challenge_id = await db_challenges.create_challenge(
                            conn, interaction.guild_id, nombre, periodo,
                            start_date, end_date, match_id, interaction.user.id,
                            premio_vip_dias=premio_vip_dias,
                            **({} if periodo != "current_match" else {"map_name": map_name, "map_start": map_start})
                        )
                        for metric, param, target in parsed_metrics:
                            await db_challenges.add_challenge_metric(conn, challenge_id, metric, target, param)

                creados.append({
                    "texto": f"✅ #{challenge_id} — {nombre}",
                    "id": challenge_id,
                    "nombre": nombre,
                    "periodo": periodo,
                    "end_date": end_date,
                    "map_name": map_name,
                    "metricas": parsed_metrics,
                })
            except Exception as e:
                errores.append(f"❌ Desafío {i+1} ({d.get('nombre','?')}): {e}")

        lines = ["**Desafíos importados:**"] + [c["texto"] for c in creados]
        if errores:
            lines += ["", "**Errores:**"] + errores

        await interaction.followup.send("\n".join(lines), ephemeral=True)

        # Notificar al canal de desafíos por cada desafío creado
        if creados:
            try:
                async with pool.acquire() as conn:
                    channel_id = await db_challenges.get_guild_challenge_channel(conn, interaction.guild_id)
                if channel_id:
                    channel = interaction.client.get_channel(channel_id) or \
                              await interaction.client.fetch_channel(channel_id)
                    for c in creados:
                        vence_hammertime = (
                            f"<t:{int(c['end_date'].timestamp())}:F> (<t:{int(c['end_date'].timestamp())}:R>)"
                            if c["end_date"] else (c["map_name"] or "Partida actual")
                        )
                        metric_bullets = "\n".join(
                            f"• {METRIC_LABELS.get(m, m)}"
                            + (f" ≥ {int(t) if t == int(t) else t}" if t else "")
                            + (f" ({p})" if p else "")
                            for m, p, t in c["metricas"]
                        )
                        embed = discord.Embed(
                            title=f"🎯 Nuevo desafío — {c['nombre']}",
                            color=0x5865F2
                        )
                        embed.add_field(name="Condición", value=metric_bullets, inline=False)
                        embed.add_field(name="Vence", value=vence_hammertime, inline=False)
                        embed.set_footer(text=f"#{c['id']} • {PERIOD_LABELS.get(c['periodo'], c['periodo'])}")
                        await channel.send(embed=embed)
            except Exception as e:
                log.warning(f"No se pudo notificar el canal de desafíos (importar): {e}")
        if creados:
            try:
                async with pool.acquire() as conn:
                    vinculados = await conn.fetch(
                        "SELECT steam_id FROM linked_players"
                    )
                if not vinculados:
                    linked = None
                    async with pool.acquire() as conn:
                        linked = await conn.fetchrow(
                            "SELECT steam_id FROM linked_players WHERE discord_id = $1",
                            int(interaction.user.id)
                        )
                    vinculados = [linked] if linked else []

                METRIC_LABELS_SIMPLE = {
                    "kills": "Kills", "deaths": "Muertes", "kd_ratio": "K/D",
                    "matches": "Partidas", "combat": "Combat", "offense": "Offense",
                    "defense": "Defense", "support": "Support",
                    "vehicles_destroyed": "Vehiculos destruidos",
                    "kills_weapon": "Kills con arma", "kills_player": "Kills a jugador",
                    "kills_type": "Kills tipo",
                }

                all_steam_ids = [
                    p for c in creados
                    for m, p, t in c["metricas"]
                    if m == "kills_player" and p
                ]
                player_names = {}
                if all_steam_ids:
                    async with pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT steam_id, player_name FROM players WHERE steam_id = ANY($1)",
                            all_steam_ids
                        )
                        player_names = {r["steam_id"]: r["player_name"] for r in rows}

                bloques = []
                for c in creados:
                    vence = format_local(c["end_date"], "%d/%m %H:%M") if c["end_date"] else (c["map_name"] or "Partida actual")
                    metricas_lines = []
                    for m, p, t in c["metricas"]:
                        label = METRIC_LABELS_SIMPLE.get(m, m)
                        obj   = int(t) if t == int(t) else t
                        param_txt = ""
                        if p:
                            if m == "kills_player":
                                param_txt = f" ({player_names.get(p, p)})"
                            else:
                                param_txt = f" ({p})"
                        metricas_lines.append(f"  {label}: {obj}{param_txt}")
                    bloques.append(
                        f"[Desafio #{c['id']}] {c['nombre']}\n"
                        + "\n".join(metricas_lines) + "\n"
                        f"Vence: {vence}\n"
                        f"Ver ranking: /hll desafio progreso"
                    )
                msg_ingame = "\n---\n".join(bloques)

                for v in vinculados:
                    try:
                        await crcon_client.message_player(
                            player_id=v["steam_id"],
                            player_name="",
                            message=msg_ingame,
                        )
                    except Exception:
                        pass
                log.info(f"[importar] mensaje in-game enviado a {len(vinculados)} vinculado(s)")
            except Exception as e:
                log.warning(f"No se pudo mandar mensaje in-game (importar): {e}", exc_info=True)