"""
snapshot_task.py
Tarea que corre dentro del bot (discord.ext.tasks) y manda, todos los días
a las 23:55 hora UY, un mensaje con el Top 10 de cada categoría del día.
Si además es domingo, agrega también el Top 10 de la semana. Si es el
último día del mes, agrega también el Top 10 del mes.

Cada "tanda" de categorías (día / semana / mes) va en SU PROPIO mensaje,
con un embed por categoría dentro de ese mensaje (Discord permite hasta
10 embeds por mensaje, y acá usamos 7 — una por categoría).

ESPERA AL MAPA EN CURSO
------------------------
Si a las 23:55 hay un mapa que YA ESTABA jugándose (arrancó antes de esa
hora), esa partida todavía no existe en la base — el collector solo
procesa partidas cerradas. Para que el snapshot del día no se mande sin
esa partida, se consulta get_public_info() y, si el mapa actual arrancó
antes del horario de corte, se espera (revisando cada minuto) a que el
mapa cambie (= la partida anterior terminó). Al detectar el cambio, se
corre un mini-collect puntual (mini_collector.py) para procesar esa
partida de inmediato, y recién entonces se manda el snapshot.

Si pasan más de WAIT_MAX_MINUTES sin que el mapa cambie (server caído,
ronda colgada, etc.), se manda el snapshot igual, sin esa partida —
salvaguarda para no trabarse indefinidamente.

Nota: una partida que arranca DESPUÉS de las 23:55 (ej. 23:58) no entra
en este mecanismo — a las 23:55 el mapa en curso era otro, así que no hay
nada que esperar para esa partida puntual. Sigue quedando fuera del
snapshot del día, igual que del día siguiente (por start_time).
"""
import calendar
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import tasks

from leaderboards import TZ_UY, build_all_category_embeds
from mini_collector import collect_new_matches

log = logging.getLogger("snapshot_task")

SNAPSHOT_HOUR = 23
SNAPSHOT_MINUTE = 55
SNAPSHOT_LIMIT = 10

# Margen de tolerancia: si el bot se reinicia y "se pierde" el minuto exacto
# (ej. estaba caído entre 23:25 y 23:35), igual disparamos el snapshot del
# día al volver a levantar, siempre que no haya pasado más de este margen
# desde el horario programado.
CATCHUP_WINDOW_MINUTES = 20

# Tope máximo de espera por el mapa en curso antes de mandar el snapshot
# igual, sin esa partida (salvaguarda ante cuelgues/bugs del server).
WAIT_MAX_MINUTES = 30


def _is_last_day_of_month(d: datetime) -> bool:
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.day == last_day


async def _get_current_map_start(crcon_client):
    """
    Devuelve el timestamp 'start' del mapa actual según get_public_info(),
    o None si no se pudo obtener (server caído, error de red, etc.).
    """
    try:
        info = await crcon_client.get_public_info()
    except Exception as e:
        log.warning(f"  get_public_info() falló: {e}")
        return None

    current_map = (info or {}).get("current_map") or {}
    return current_map.get("start")


async def _send_snapshot(bot, pool, guild_id: int, channel_id: int,
                          period_value: str, title_prefix: str,
                          reference_date: datetime = None):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning(f"  No pude resolver el canal {channel_id} (guild {guild_id})")
            return

    embeds = await build_all_category_embeds(
        pool, period_value, SNAPSHOT_LIMIT, include_links=False, reference_date=reference_date
    )
    if not embeds:
        log.info(f"  Sin datos para snapshot '{period_value}' en guild {guild_id}, se omite")
        return

    # Discord permite máximo 10 embeds por mensaje; tenemos 7 categorías, entra justo.
    try:
        await channel.send(content=f"**{title_prefix}**", embeds=embeds)
        log.info(f"  Snapshot '{period_value}' enviado a guild {guild_id} (canal {channel_id})")
    except discord.HTTPException as e:
        log.error(f"  Error enviando snapshot '{period_value}' a guild {guild_id}: {e}")


async def run_snapshots_for_all_guilds(bot, pool, now_uy: datetime):
    """Recorre todos los guilds configurados con snapshot_channel_id y manda lo que corresponda."""
    async with pool.acquire() as conn:
        guild_rows = await conn.fetch(
            "SELECT guild_id, snapshot_channel_id FROM guild_config WHERE snapshot_channel_id IS NOT NULL"
        )

    if not guild_rows:
        log.info("  Ningún guild tiene canal de snapshots configurado, nada que hacer")
        return

    is_sunday = now_uy.weekday() == 6  # lunes=0 ... domingo=6
    is_month_end = _is_last_day_of_month(now_uy)
    fecha_txt = now_uy.strftime("%d/%m/%Y")

    for g in guild_rows:
        guild_id = g["guild_id"]
        channel_id = g["snapshot_channel_id"]

        # Día: siempre
        await _send_snapshot(
            bot, pool, guild_id, channel_id, "day",
            f"📅 Resumen del día — {fecha_txt}"
        )

        # Semana: solo domingos
        if is_sunday:
            await _send_snapshot(
                bot, pool, guild_id, channel_id, "week",
                f"🗓️ Resumen de la semana — cierra {fecha_txt}"
            )

        # Mes: solo el último día del mes
        if is_month_end:
            await _send_snapshot(
                bot, pool, guild_id, channel_id, "month",
                f"📆 Resumen del mes — cierra {fecha_txt}"
            )


async def run_snapshot_manual(bot, pool, guild_id: int, channel_id: int, period_value: str,
                               reference_date: datetime = None):
    """
    Dispara un solo snapshot (día/semana/mes) para UN guild puntual, sin
    pasar por las condiciones de día/hora ni la espera del mapa en curso.
    Pensado para ser llamado desde un comando admin (/hlladmin snapshot).

    reference_date: si se pasa, el período (día/semana/mes) se calcula en
    base a esa fecha en vez de "ahora" — permite pedir el snapshot de un
    día/semana/mes pasado (o futuro) puntual.
    """
    now_uy = datetime.now(TZ_UY)

    if reference_date is not None:
        fecha_ref_txt = reference_date.strftime("%d/%m/%Y")
        title_map = {
            "day":   f"📅 Resumen del día — {fecha_ref_txt} (manual)",
            "week":  f"🗓️ Resumen de la semana que contiene el {fecha_ref_txt} (manual)",
            "month": f"📆 Resumen del mes de {fecha_ref_txt} (manual)",
        }
    else:
        fecha_txt = now_uy.strftime("%d/%m/%Y")
        title_map = {
            "day":   f"📅 Resumen del día — {fecha_txt} (manual)",
            "week":  f"🗓️ Resumen de la semana — {fecha_txt} (manual)",
            "month": f"📆 Resumen del mes — {fecha_txt} (manual)",
        }

    await _send_snapshot(
        bot, pool, guild_id, channel_id, period_value, title_map[period_value], reference_date
    )


def setup_snapshot_task(bot, pool, crcon_client):
    """
    Registra la tarea de loop que chequea cada minuto si es la hora de
    disparar el snapshot (23:55 hora UY). Se debe llamar una vez al
    iniciar el bot, ej: setup_snapshot_task(bot, pool, crcon).start()

    crcon_client: instancia de CRCONClient ya inicializada (con start()
    corrido), usada para consultar get_public_info() y, si hace falta,
    correr el mini-collect puntual.
    """
    last_fired_date = {"value": None}    # evita disparar 2 veces el mismo día

    # Estado de "esperando que termine el mapa en curso". None = no estamos
    # esperando nada en este momento.
    wait_state = {"baseline_start": None, "started_at": None}

    async def _fire_snapshots(now_uy: datetime, today):
        log.info(f"Disparando snapshots del {today} (hora UY: {now_uy.strftime('%H:%M:%S')})")
        try:
            await run_snapshots_for_all_guilds(bot, pool, now_uy)
        except Exception as e:
            log.error(f"Error en snapshot_loop: {e}", exc_info=True)
            await bot._send_status(f"⚠️ **Error en bot** (snapshot loop)\n```{type(e).__name__}: {e}```")
        last_fired_date["value"] = today
        wait_state["baseline_start"] = None
        wait_state["started_at"] = None

    @tasks.loop(minutes=1)
    async def snapshot_loop():
        now_uy = datetime.now(TZ_UY)
        today = now_uy.date()

        if last_fired_date["value"] == today:
            return  # ya se disparó hoy

        # ── Si ya estamos esperando que termine el mapa en curso ──────
        if wait_state["started_at"] is not None:
            elapsed_min = (now_uy - wait_state["started_at"]).total_seconds() / 60

            current_start = await _get_current_map_start(crcon_client)

            # Si pudimos leer el mapa actual y CAMBIÓ respecto al que
            # vimos al empezar a esperar -> la partida anterior terminó.
            if current_start is not None and current_start != wait_state["baseline_start"]:
                log.info(
                    f"Mapa en curso cambió (esperamos {elapsed_min:.0f} min); "
                    f"corriendo mini-collect antes del snapshot del {today}"
                )
                try:
                    new_count = await collect_new_matches(crcon_client, pool)
                    log.info(f"  mini-collect: {new_count} partida(s) nueva(s) procesada(s)")
                except Exception as e:
                    log.error(f"  Error en mini-collect: {e}", exc_info=True)

                await _fire_snapshots(now_uy, today)
                return

            # Todavía no cambió -> seguimos esperando, salvo que se nos
            # haya agotado el tope máximo.
            if elapsed_min >= WAIT_MAX_MINUTES:
                log.warning(
                    f"Pasaron {elapsed_min:.0f} min esperando que termine el mapa en curso "
                    f"(tope {WAIT_MAX_MINUTES} min); mando el snapshot del {today} sin esa partida."
                )
                await _fire_snapshots(now_uy, today)
                return

            return  # seguimos esperando, nada más que hacer este minuto

        # ── Todavía no llegó la hora objetivo de hoy ───────────────────
        target_today = now_uy.replace(
            hour=SNAPSHOT_HOUR, minute=SNAPSHOT_MINUTE, second=0, microsecond=0
        )
        if now_uy < target_today:
            return

        minutes_late = (now_uy - target_today).total_seconds() / 60
        if minutes_late > CATCHUP_WINDOW_MINUTES:
            # El bot estuvo caído un buen rato y nunca llegó a disparar hoy;
            # nos lo saltamos para no mandar algo con horas de atraso.
            log.warning(
                f"Se perdió la ventana de snapshot de hoy ({today}); "
                f"pasaron {minutes_late:.0f} min del horario objetivo. Se omite hasta mañana."
            )
            last_fired_date["value"] = today
            return

        # ── Llegó la hora: ¿hay un mapa en curso que arrancó ANTES? ────
        current_start = await _get_current_map_start(crcon_client)

        if current_start is None:
            # No pudimos leer el estado del server; no tiene sentido
            # esperar por algo que no podemos verificar -> mandamos directo.
            log.warning("No se pudo leer get_public_info() a la hora del snapshot; se manda sin esperar.")
            await _fire_snapshots(now_uy, today)
            return

        target_ts = target_today.timestamp()
        if current_start < target_ts:
            # El mapa actual ya estaba jugándose antes de las 23:55 ->
            # esperamos a que termine antes de mandar el snapshot.
            log.info(
                f"Hay un mapa en curso que arrancó antes de las {SNAPSHOT_HOUR}:{SNAPSHOT_MINUTE:02d}; "
                f"esperando a que termine antes de mandar el snapshot del {today}."
            )
            wait_state["baseline_start"] = current_start
            wait_state["started_at"] = now_uy
            return

        # El mapa actual arrancó EN o DESPUÉS del horario de corte -> no
        # hay nada que esperar, mandamos el snapshot normalmente.
        await _fire_snapshots(now_uy, today)

    return snapshot_loop