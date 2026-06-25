"""
snapshot_task.py
Tarea que corre dentro del bot (discord.ext.tasks) y manda, todos los días
a las 23:29 hora UY, un mensaje con el Top 10 de cada categoría del día.
Si además es domingo, agrega también el Top 10 de la semana. Si es el
último día del mes, agrega también el Top 10 del mes.

Cada "tanda" de categorías (día / semana / mes) va en SU PROPIO mensaje,
con un embed por categoría dentro de ese mensaje (Discord permite hasta
10 embeds por mensaje, y acá usamos 7 — una por categoría).
"""
import calendar
import logging
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import tasks

from leaderboards import TZ_UY, build_all_category_embeds

log = logging.getLogger("snapshot_task")

SNAPSHOT_HOUR = 23
SNAPSHOT_MINUTE = 29
SNAPSHOT_LIMIT = 10

# Margen de tolerancia: si el bot se reinicia y "se pierde" el minuto exacto
# (ej. estaba caído entre 23:25 y 23:35), igual disparamos el snapshot del
# día al volver a levantar, siempre que no haya pasado más de este margen
# desde el horario programado.
CATCHUP_WINDOW_MINUTES = 20


def _is_last_day_of_month(d: datetime) -> bool:
    last_day = calendar.monthrange(d.year, d.month)[1]
    return d.day == last_day


async def _send_snapshot(bot, pool, guild_id: int, channel_id: int,
                          period_value: str, title_prefix: str):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning(f"  No pude resolver el canal {channel_id} (guild {guild_id})")
            return

    embeds = await build_all_category_embeds(pool, period_value, SNAPSHOT_LIMIT)
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


async def run_snapshot_manual(bot, pool, guild_id: int, channel_id: int, period_value: str):
    """
    Dispara un solo snapshot (día/semana/mes) para UN guild puntual, sin
    pasar por las condiciones de día/hora del loop automático. Pensado
    para ser llamado desde un comando admin (/hll snapshot).
    """
    now_uy = datetime.now(TZ_UY)
    fecha_txt = now_uy.strftime("%d/%m/%Y")
    title_map = {
        "day":   f"📅 Resumen del día — {fecha_txt} (manual)",
        "week":  f"🗓️ Resumen de la semana — {fecha_txt} (manual)",
        "month": f"📆 Resumen del mes — {fecha_txt} (manual)",
    }
    await _send_snapshot(bot, pool, guild_id, channel_id, period_value, title_map[period_value])


def setup_snapshot_task(bot, pool):
    """
    Registra la tarea de loop que chequea cada minuto si es la hora de
    disparar el snapshot (23:29 hora UY). Se debe llamar una vez al
    iniciar el bot, ej: setup_snapshot_task(bot, pool).start()
    """
    last_fired_date = {"value": None}  # evita disparar 2 veces el mismo día

    @tasks.loop(minutes=1)
    async def snapshot_loop():
        now_uy = datetime.now(TZ_UY)
        today = now_uy.date()

        if last_fired_date["value"] == today:
            return  # ya se disparó hoy

        target_today = now_uy.replace(
            hour=SNAPSHOT_HOUR, minute=SNAPSHOT_MINUTE, second=0, microsecond=0
        )

        # Disparamos si estamos en o después del horario objetivo, pero dentro
        # de la ventana de tolerancia (para no perder el snapshot si el bot
        # se reinició justo en ese momento).
        if now_uy >= target_today:
            minutes_late = (now_uy - target_today).total_seconds() / 60
            if minutes_late <= CATCHUP_WINDOW_MINUTES:
                log.info(f"Disparando snapshots del {today} (hora UY: {now_uy.strftime('%H:%M:%S')})")
                try:
                    await run_snapshots_for_all_guilds(bot, pool, now_uy)
                except Exception as e:
                    log.error(f"Error en snapshot_loop: {e}", exc_info=True)
                last_fired_date["value"] = today
            else:
                # Pasaron más de CATCHUP_WINDOW_MINUTES del horario y nunca se
                # disparó (ej. el bot estuvo caído varias horas) -> nos lo
                # saltamos para no mandar un snapshot con horas de atraso.
                log.warning(
                    f"Se perdió la ventana de snapshot de hoy ({today}); "
                    f"pasaron {minutes_late:.0f} min del horario objetivo. Se omite hasta mañana."
                )
                last_fired_date["value"] = today

    return snapshot_loop