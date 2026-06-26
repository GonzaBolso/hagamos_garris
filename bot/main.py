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

    async def on_ready(self):
        log.info(f"Bot conectado como {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Hell Let Loose 🪖"
            )
        )

    async def close(self):
        if hasattr(self, "snapshot_loop"):
            self.snapshot_loop.cancel()
        await crcon.close()
        await super().close()


async def main():
    pool = await asyncpg.create_pool(config.DB_DSN, min_size=2, max_size=10)
    bot  = HLLBot(pool)

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())