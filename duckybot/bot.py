"""De Discord-client met gedeelde helpers voor alle cogs."""
import logging
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from . import __version__
from .config import Config
from .jellyfin import Jellyfin
from .jellyseerr import Jellyseerr
from .storage import LinkStore

log = logging.getLogger(__name__)

EXTENSIONS = ("duckybot.cogs.media", "duckybot.cogs.invites", "duckybot.cogs.notifications")

COLOR_BRAND = 0xF5C518   # eend-geel
COLOR_OK = 0x57F287
COLOR_INFO = 0x3498DB
COLOR_WARN = 0xFFA500
COLOR_ERROR = 0xED4245


def seerr_name(user: dict[str, Any]) -> str:
    return (
        user.get("displayName")
        or user.get("username")
        or user.get("jellyfinUsername")
        or user.get("plexUsername")
        or user.get("email")
        or f"gebruiker #{user.get('id')}"
    )


class DuckyBot(commands.Bot):
    def __init__(self, config: Config):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.config = config
        self.links = LinkStore(config.data_dir)
        self.http_session: aiohttp.ClientSession | None = None
        self.seerr: Jellyseerr
        self.jellyfin: Jellyfin | None = None
        self._web_runner = None

    @property
    def guild_object(self) -> discord.Object:
        return discord.Object(id=self.config.guild_id)

    async def setup_hook(self) -> None:
        from .web import start_web_server

        self.http_session = aiohttp.ClientSession()
        self.seerr = Jellyseerr(self.http_session, self.config.jellyseerr_url, self.config.jellyseerr_api_key)
        if self.config.jellyfin_enabled:
            self.jellyfin = Jellyfin(self.http_session, self.config.jellyfin_url, self.config.jellyfin_api_key)

        self.tree.on_error = self.on_app_command_error
        for ext in EXTENSIONS:
            await self.load_extension(ext)

        # Alleen voor de eigen server registreren: dan zijn commando's direct beschikbaar.
        self.tree.copy_global_to(guild=self.guild_object)
        synced = await self.tree.sync(guild=self.guild_object)
        log.info("%d slash-commando's gesynchroniseerd", len(synced))

        self._web_runner = await start_web_server(self)

    async def close(self) -> None:
        if self._web_runner:
            await self._web_runner.cleanup()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Ingelogd als %s (%s), versie %s", self.user, self.user.id if self.user else "?", __version__)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="/aanvraag"))

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ) -> None:
        if isinstance(error, discord.app_commands.CheckFailure):
            return  # de check heeft zelf al geantwoord
        log.error("Fout in /%s", interaction.command.name if interaction.command else "?", exc_info=error)
        text = "❌ Er ging iets mis. Probeer het later opnieuw."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    # --- rechten ---------------------------------------------------------

    def is_admin(self, member: discord.abc.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_guild:
            return True
        return any(role.id in self.config.admin_role_ids for role in member.roles)

    def can_invite(self, member: discord.abc.User) -> bool:
        if self.is_admin(member):
            return True
        return isinstance(member, discord.Member) and any(
            role.id in self.config.invite_role_ids for role in member.roles
        )

    # --- koppelingen -----------------------------------------------------

    async def resolve_seerr_user(self, user: discord.abc.User) -> dict[str, Any] | None:
        """Jellyseerr-gebruiker bij een Discord-gebruiker: eerst lokaal, dan via het profiel-Discord-ID."""
        link = self.links.get(user.id)
        if link:
            return {"id": link["seerr_id"], "displayName": link["name"]}
        found = await self.seerr.find_user_by_discord_id(str(user.id))
        if found:
            self.links.set(user.id, found["id"], seerr_name(found))
        return found

    # --- berichten -------------------------------------------------------

    async def channel(self, channel_id: int) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                log.warning("Kanaal %s niet bereikbaar: %s", channel_id, exc)
                return None
        return channel  # type: ignore[return-value]

    async def send_to(self, channel_id: int, **kwargs) -> discord.Message | None:
        channel = await self.channel(channel_id)
        if channel is None:
            return None
        try:
            return await channel.send(**kwargs)
        except discord.HTTPException as exc:
            log.warning("Versturen naar kanaal %s mislukt: %s", channel_id, exc)
            return None

    async def dm(self, user_id: int, **kwargs) -> bool:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(**kwargs)
            return True
        except discord.HTTPException as exc:
            log.info("DM naar %s mislukt: %s", user_id, exc)
            return False
