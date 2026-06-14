"""
checks.py — Decoradores de permisos para los comandos del bot.

Roles:
  - Admin: puede usar todo en cualquier canal
  - Player: puede usar comandos permitidos solo en el canal configurado
"""
import discord
from discord import app_commands
import asyncpg


async def get_guild_config(pool: asyncpg.Pool, guild_id: int) -> dict:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM guild_config WHERE guild_id = $1", guild_id
        )
    return dict(row) if row else {}


def is_admin(member: discord.Member, admin_role_id: int) -> bool:
    if not admin_role_id:
        return member.guild_permissions.administrator
    return any(r.id == admin_role_id for r in member.roles) or member.guild_permissions.administrator


def is_player(member: discord.Member, player_role_id: int) -> bool:
    if not player_role_id:
        return True  # sin restricción de rol
    return any(r.id == player_role_id for r in member.roles)


def admin_only():
    """Solo admins, en cualquier canal."""
    async def predicate(interaction: discord.Interaction) -> bool:
        config = await get_guild_config(interaction.client.pool, interaction.guild_id)
        admin_role_id = config.get("admin_role_id")

        if not is_admin(interaction.user, admin_role_id):
            await interaction.response.send_message(
                "❌ No tenés permisos para usar este comando.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def player_or_admin():
    """Players en el canal configurado, admins en cualquier lado."""
    async def predicate(interaction: discord.Interaction) -> bool:
        config = await get_guild_config(interaction.client.pool, interaction.guild_id)
        admin_role_id  = config.get("admin_role_id")
        player_role_id = config.get("mod_role_id")
        channel_id     = config.get("stats_channel_id")

        # Admin: pasa siempre
        if is_admin(interaction.user, admin_role_id):
            return True

        # Verificar rol de player
        if not is_player(interaction.user, player_role_id):
            await interaction.response.send_message(
                "❌ No tenés el rol necesario para usar este comando.", ephemeral=True
            )
            return False

        # Verificar canal
        if channel_id and interaction.channel_id != channel_id:
            channel = interaction.guild.get_channel(channel_id)
            mention = channel.mention if channel else f"<#{channel_id}>"
            await interaction.response.send_message(
                f"❌ Este comando solo se puede usar en {mention}.", ephemeral=True
            )
            return False

        return True
    return app_commands.check(predicate)