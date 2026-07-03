"""
Bot principal — conecta todo.
"""
import asyncio
import os
import logging

import asyncpg
import discord
from discord.ext import commands

import config
from api import crcon
from commands.hll import setup_hll
from commands.stats import setup_stats
from commands.challenges import setup_challenges
from snapshot_task import setup_snapshot_task
from challenge_close_task import setup_challenge_close_task
from event_notifier_task import setup_event_notifier_task
from server_status_task import setup_server_status_task

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [bot] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# discord.py es muy ruidoso en DEBUG; lo dejamos en WARNING siempre,
# salvo que se pida explícitamente con DISCORD_LOG_LEVEL
discord_level = os.environ.get("DISCORD_LOG_LEVEL", "WARNING").upper()
logging.getLogger("discord").setLevel(getattr(logging, discord_level, logging.WARNING))


class HLLBot(commands.Bot):
    def __init__(self, pool: asyncpg.Pool):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool = pool

    async def setup_hook(self):
        await crcon.start()
        hll_group, hlladmin_group = setup_hll(self, self.pool)
        setup_challenges(hll_group, hlladmin_group, self.pool, crcon)
        self.tree.add_command(hll_group)
        self.tree.add_command(hlladmin_group)
        setup_stats(self, self.pool)

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands sincronizados")

        async def on_tree_error(interaction: discord.Interaction, error):
            if isinstance(error, discord.app_commands.CheckFailure):
                return  # los checks ya enviaron su propio mensaje
            log.error(f"Error en comando: {error}", exc_info=error)
            cmd = getattr(interaction.command, "name", "?")
            await self._send_status(f"⚠️ **Error en bot** (comando `/{cmd}`)\n```{type(error).__name__}: {error}```")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Ocurrió un error inesperado.", ephemeral=True)
                else:
                    await interaction.followup.send("❌ Ocurrió un error inesperado.", ephemeral=True)
            except Exception:
                pass

        self.tree.on_error = on_tree_error

        self.snapshot_loop = setup_snapshot_task(self, self.pool, crcon)
        self.snapshot_loop.start()
        log.info("Tarea de snapshots automáticos iniciada (23:55 hora UY)")

        self.challenge_close_loop = setup_challenge_close_task(self, self.pool)
        self.challenge_close_loop.start()
        log.info("Tarea de notificación de cierre de desafíos iniciada (cada 1 min)")

        self.event_notifier_loop = setup_event_notifier_task(self, self.pool)
        self.event_notifier_loop.start()
        log.info("Tarea de notificación de eventos destacados iniciada (cada 20s)")

        self.server_status_loop = setup_server_status_task(self, self.pool)
        log.info("Tarea de estado del servidor iniciada (cada 60s)")

    async def on_ready(self):
        log.info(f"Bot conectado como {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Hell Let Loose 🪖"
            )
        )
        await self._send_status("🟢 **Bot conectado** y listo.")

    async def _send_status(self, message: str):
        """Manda un mensaje al canal de status via webhook (confiable en
        on_ready y close) o via canal si no hay webhook configurado."""
        if config.STATUS_WEBHOOK_URL:
            try:
                import aiohttp as _aiohttp
                async with _aiohttp.ClientSession() as _s:
                    await _s.post(config.STATUS_WEBHOOK_URL, json={"content": message})
                return
            except Exception as e:
                log.warning(f"Webhook de status falló: {e}")
        if not config.STATUS_CHANNEL_ID:
            return
        try:
            channel = self.get_channel(config.STATUS_CHANNEL_ID) or                       await self.fetch_channel(config.STATUS_CHANNEL_ID)
            await channel.send(message)
        except Exception as e:
            log.warning(f"No pude mandar mensaje de status: {e}")

    async def close(self):
        if hasattr(self, "snapshot_loop"):
            self.snapshot_loop.cancel()
        if hasattr(self, "challenge_close_loop"):
            self.challenge_close_loop.cancel()
        if hasattr(self, "event_notifier_loop"):
            self.event_notifier_loop.cancel()
        if hasattr(self, "server_status_loop"):
            self.server_status_loop.cancel()
        await crcon.close()
        await super().close()


async def main():
    pool = await asyncpg.create_pool(config.DB_DSN, min_size=2, max_size=10)
    bot  = HLLBot(pool)

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


def _send_disconnect_sync():
    if not config.STATUS_WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(
            config.STATUS_WEBHOOK_URL,
            json={"content": "🔴 **Bot desconectado**."},
            timeout=3,
        )
    except Exception as e:
        log.warning(f"Webhook desconexión falló: {e} — URL: {config.STATUS_WEBHOOK_URL[:50]}...")


def _sigterm_handler(signum, frame):
    log.info("SIGTERM recibido, mandando webhook de desconexión...")
    _send_disconnect_sync()
    raise SystemExit(0)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _sigterm_handler)
    asyncio.run(main())