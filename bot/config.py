import os

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID      = int(os.environ["GUILD_ID"])

CRCON_URL     = os.environ["CRCON_URL"].rstrip("/")
CRCON_API_KEY = os.environ["CRCON_API_KEY"]

DB_DSN = (
    f"postgresql://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
    f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
)
