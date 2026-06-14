"""
Bot principal — conecta todo.
"""
import asyncio
import logging

import asyncpg
import discord
from discord.ext import commands

import config
from api import crcon
from commands.hll import setup_hll
from commands.stats import setup_stats

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bot] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class HLLBot(commands.Bot):
    def __init__(self, pool: asyncpg.Pool):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool = pool

    async def setup_hook(self):
        await crcon.start()
        setup_hll(self, self.pool)
        setup_stats(self, self.pool)

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands sincronizados")

    async def on_ready(self):
        log.info(f"Bot conectado como {self.user} (ID: {self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Hell Let Loose 🪖"
            )
        )

    async def close(self):
        await crcon.close()
        await super().close()


async def main():
    pool = await asyncpg.create_pool(config.DB_DSN, min_size=2, max_size=10)
    bot  = HLLBot(pool)

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
