"""Notificaties: Jellyseerr-webhooks, /melding en het generieke /notify-endpoint."""
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import COLOR_BRAND, COLOR_ERROR, COLOR_OK, DuckyBot
from ..jellyseerr import JellyseerrError
from ..notify import Notice, parse_seerr_payload

log = logging.getLogger(__name__)


def parse_color(value: Any) -> int:
    """Kleur als getal (3447003) of hex-string ("#3498db")."""
    try:
        if isinstance(value, str):
            return int(value.lstrip("#"), 16)
        if isinstance(value, int):
            return value
    except ValueError:
        pass
    return COLOR_BRAND


class RequestDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"seerr:(?P<action>approve|decline):(?P<id>\d+)",
):
    """Goedkeuren/afwijzen-knop onder een nieuwe aanvraag; blijft werken na een herstart."""

    def __init__(self, action: str, request_id: int):
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Goedkeuren" if approve else "Afwijzen",
                emoji="✅" if approve else "✖️",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"seerr:{action}:{request_id}",
            )
        )
        self.action = action
        self.request_id = request_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: DuckyBot = interaction.client  # type: ignore[assignment]
        if not bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen aanvragen beoordelen.", ephemeral=True)
            return
        await interaction.response.defer()

        # Liefst namens het gekoppelde account van de admin, zodat Jellyseerr toont wie het deed.
        link = bot.links.get(interaction.user.id)
        try:
            try:
                await bot.seerr.set_request_status(self.request_id, self.action, as_user=link["seerr_id"] if link else None)
            except JellyseerrError as exc:
                if link and exc.status == 403:
                    await bot.seerr.set_request_status(self.request_id, self.action)
                else:
                    raise
        except JellyseerrError as exc:
            await interaction.followup.send(f"❌ Dat lukte niet: {exc.message}", ephemeral=True)
            return

        approved = self.action == "approve"
        message = interaction.message
        if message and message.embeds:
            embed = message.embeds[0]
            embed.color = COLOR_OK if approved else COLOR_ERROR
            embed.add_field(
                name="Goedgekeurd door" if approved else "Afgewezen door", value=interaction.user.mention, inline=False
            )
            await message.edit(embed=embed, view=None)


class AnnouncementModal(discord.ui.Modal, title="Melding versturen"):
    titel = discord.ui.TextInput(label="Titel", max_length=256)
    bericht = discord.ui.TextInput(label="Bericht", style=discord.TextStyle.paragraph, max_length=4000)

    def __init__(self, channel: discord.abc.Messageable, ping: discord.Role | None):
        super().__init__()
        self.channel = channel
        self.ping = ping

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title=self.titel.value, description=self.bericht.value, color=COLOR_BRAND)
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        content, allowed = None, discord.AllowedMentions.none()
        if self.ping is not None:
            if self.ping.is_default():
                content, allowed = "@everyone", discord.AllowedMentions(everyone=True)
            else:
                content, allowed = self.ping.mention, discord.AllowedMentions(roles=[self.ping])
        try:
            await self.channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"❌ Versturen mislukt: {exc.text}", ephemeral=True)
            return
        await interaction.response.send_message("📣 Melding verstuurd.", ephemeral=True)


class Notifications(commands.Cog):
    def __init__(self, bot: DuckyBot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RequestDecisionButton)

    # --- Jellyseerr-webhook ----------------------------------------------

    def build_embed(self, notice: Notice) -> discord.Embed:
        cfg = self.bot.config
        url = None
        if notice.media_type in ("movie", "tv") and notice.tmdb_id:
            url = f"{cfg.jellyseerr_public_url}/{notice.media_type}/{notice.tmdb_id}"
        embed = discord.Embed(title=notice.title, description=notice.description or None, color=notice.color, url=url)
        embed.set_author(name=notice.heading)
        if notice.image:
            embed.set_thumbnail(url=notice.image)
        if notice.requester:
            embed.add_field(name="Aangevraagd door", value=notice.requester)
        for name, value in notice.extra[:10]:
            embed.add_field(name=name[:256], value=value[:1024])
        return embed

    def jellyfin_url(self, notice: Notice) -> str | None:
        base = self.bot.config.jellyfin_public_url
        if notice.jellyfin_item_id and base:
            return f"{base}/web/#/details?id={notice.jellyfin_item_id}"
        return None

    async def handle_seerr_webhook(self, payload: dict[str, Any]) -> Notice:
        notice = parse_seerr_payload(payload)
        cfg = self.bot.config
        embed = self.build_embed(notice)

        watch_view = None
        if link := self.jellyfin_url(notice):
            watch_view = discord.ui.View()
            watch_view.add_item(discord.ui.Button(label="Kijk in Jellyfin", url=link))

        if "admin" in notice.targets:
            view = None
            if notice.notification_type == "MEDIA_PENDING" and notice.request_id:
                view = discord.ui.View(timeout=None)
                view.add_item(RequestDecisionButton("approve", notice.request_id))
                view.add_item(RequestDecisionButton("decline", notice.request_id))
            await self.bot.send_to(cfg.admin_channel_id or cfg.request_channel_id, embed=embed, view=view)

        if "public" in notice.targets:
            await self.bot.send_to(cfg.request_channel_id, embed=embed, view=watch_view)

        if "dm" in notice.targets and notice.dm_text:
            recipients = set(notice.requester_discord_ids)
            if notice.requester:
                recipients.update(self.bot.links.discord_ids_for(name=notice.requester))
            for user_id in recipients:
                await self.bot.dm(user_id, content=notice.dm_text, embed=embed, view=watch_view)

        log.info("Jellyseerr-melding %s verwerkt (%s)", notice.notification_type, notice.title)
        return notice

    # --- generiek /notify-endpoint ----------------------------------------

    async def handle_notify(self, payload: dict[str, Any]) -> bool:
        """Melding van een andere dienst: {title, message, channel_id?, user_id?, color?, url?, image?, mention_role_id?}."""
        title = str(payload.get("title") or "").strip()[:256]
        message = str(payload.get("message") or payload.get("content") or "").strip()[:4000]
        if not title and not message:
            raise ValueError("title of message is verplicht")
        color = parse_color(payload.get("color"))
        embed = discord.Embed(title=title or None, description=message or None, color=color, url=payload.get("url") or None)
        if payload.get("image"):
            embed.set_thumbnail(url=str(payload["image"]))

        if payload.get("user_id"):
            return await self.bot.dm(int(payload["user_id"]), embed=embed)

        content, allowed = None, discord.AllowedMentions.none()
        if role_id := payload.get("mention_role_id"):
            content, allowed = f"<@&{int(role_id)}>", discord.AllowedMentions(roles=[discord.Object(int(role_id))])
        channel_id = int(payload.get("channel_id") or self.bot.config.announce_channel_id or self.bot.config.request_channel_id)
        return await self.bot.send_to(channel_id, content=content, embed=embed, allowed_mentions=allowed) is not None

    # --- /melding --------------------------------------------------------

    @app_commands.command(name="melding", description="(Admin) Verstuur een aankondiging")
    @app_commands.describe(kanaal="Standaard het aankondigingskanaal", ping="Optioneel een rol (of @everyone) om te pingen")
    @app_commands.default_permissions(manage_guild=True)
    async def melding(
        self,
        interaction: discord.Interaction,
        kanaal: discord.TextChannel | None = None,
        ping: discord.Role | None = None,
    ) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen meldingen versturen.", ephemeral=True)
            return
        channel = kanaal or await self.bot.channel(self.bot.config.announce_channel_id) or interaction.channel
        if channel is None:
            await interaction.response.send_message("Geen kanaal gevonden om naartoe te sturen.", ephemeral=True)
            return
        await interaction.response.send_modal(AnnouncementModal(channel, ping))


async def setup(bot: DuckyBot) -> None:
    await bot.add_cog(Notifications(bot))
