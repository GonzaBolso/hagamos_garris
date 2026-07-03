"""config.py — Variables de entorno del collector."""
import os

CRCON_URL            = os.environ["CRCON_URL"].rstrip("/")
CRCON_API_KEY        = os.environ.get("CRCON_API_KEY", "")
INTERVAL             = int(os.environ.get("COLLECT_INTERVAL_MINUTES", 30)) * 60
BACKFILL_MATCH_STATS = os.environ.get("BACKFILL_MATCH_STATS", "").lower() in ("1", "true", "yes")
STATUS_WEBHOOK_URL   = os.environ.get("STATUS_WEBHOOK_URL", "")

DB_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

HEADERS = {"Content-Type": "application/json"}
if CRCON_API_KEY:
    HEADERS["Authorization"] = f"Bearer {CRCON_API_KEY}"

LIVE_POLL_INTERVAL_SECONDS    = 25
EVENT_DETECTOR_INTERVAL_SECONDS = 25

# Armas de cuerpo a cuerpo (un kill con estas se considera "fakeo")
MELEE_WEAPONS = {"M3 KNIFE", "FELDSPATEN", "MPL-50 SPADE", "Fairbairn\u2013Sykes"}