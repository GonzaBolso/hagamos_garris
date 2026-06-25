"""
timeutils.py — Conversión centralizada de UTC a la zona horaria local del bot.
Todas las fechas en la BD y en la API de CRCON vienen en UTC; este módulo
las convierte para mostrarlas al usuario.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "America/Argentina/Buenos_Aires"))


def to_local(dt: datetime) -> datetime:
    """Convierte un datetime (naive o aware) a la zona horaria local configurada."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(LOCAL_TZ)


def format_local(dt: datetime, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Convierte a hora local y formatea. Devuelve '?' si dt es None."""
    if dt is None:
        return "?"
    return to_local(dt).strftime(fmt)


def parse_iso_to_local(iso_str: str, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Parsea un string ISO (de la API de CRCON) y lo formatea en hora local."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str)
        return format_local(dt, fmt)
    except ValueError:
        return iso_str