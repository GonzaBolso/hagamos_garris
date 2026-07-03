import os

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])

CRCON_URL     = os.environ["CRCON_URL"].rstrip("/")
CRCON_API_KEY = os.environ["CRCON_API_KEY"]

DB_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)

# Canal de Discord donde se mandan mensajes de estado del bot (opcional)
_status = os.environ.get("STATUS_CHANNEL_ID", "")
STATUS_CHANNEL_ID = int(_status) if _status.strip().isdigit() else None

# Webhook del mismo canal de status (más confiable que get_channel en on_ready/close)
STATUS_WEBHOOK_URL = os.environ.get("STATUS_WEBHOOK_URL", "")