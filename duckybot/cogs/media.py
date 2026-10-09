"""Media-aanvragen via Jellyseerr, en het koppelen van Discord- aan Jellyseerr-accounts."""
import asyncio
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import COLOR_BRAND, DuckyBot, seerr_name
from ..jellyseerr import (
    MEDIA_STATUS,
    REQUEST_STATUS,
    JellyseerrError,
    poster_of,
    requestable_seasons,
    title_of,
    year_of,
)

log = logging.getLogger(__name__)

CHOICE_RE = re.compile(r"^(movie|tv):(\d+)$")
TYPE_LABEL = {"movie": "🎬 Film", "tv": "📺 Serie"}
STATUS_AVAILABLE = 5


def not_linked_message(bot: DuckyBot) -> str:
    url = bot.config.jellyseerr_public_url
    return (
        "Je Discord-account is nog niet gekoppeld aan een Jellyseerr-account.\n"
        f"Log in op {url}, ga naar **Profiel → Instellingen → Notificaties → Discord** en vul je "
        "Discord-gebruikers-ID in (rechtsklik op jezelf → *Gebruikers-ID kopiëren*). "
        "Of vraag een admin om `/koppel`."
    )


def jellyfin_link(bot: DuckyBot, media: dict[str, Any]) -> str | None:
    item_id = (media.get("mediaInfo") or {}).get("jellyfinMediaId")
    if item_id and bot.config.jellyfin_public_url:
        return f"{bot.config.jellyfin_public_url}/web/#/details?id={item_id}"
    return None


def media_embed(bot: DuckyBot, media_type: str, media: dict[str, Any]) -> discord.Embed:
    title = title_of(media)
    year = year_of(media)
    overview = media.get("overview") or "Geen beschrijving beschikbaar."
    if len(overview) > 600:
        overview = overview[:597] + "..."
    embed = discord.Embed(
        title=f"{title} ({year})" if year else title,
        description=overview,
        color=COLOR_BRAND,
        url=f"{bot.config.jellyseerr_public_url}/{media_type}/{media['id']}",
    )
    embed.add_field(name="Type", value=TYPE_LABEL.get(media_type, media_type))
    status = (media.get("mediaInfo") or {}).get("status", 1)
    embed.add_field(name="Status", value=MEDIA_STATUS.get(status, "Onbekend"))
    if media_type == "tv":
        seasons = [s for s in media.get("seasons") or [] if s.get("seasonNumber", 0) > 0]
        embed.add_field(name="Seizoenen", value=str(len(seasons)))
    if media.get("voteAverage"):
        embed.add_field(name="TMDB", value=f"⭐ {media['voteAverage']:.1f}")
    if poster := poster_of(media):
        embed.set_thumbnail(url=poster)
    return embed


class RequestView(discord.ui.View):
    def __init__(self, bot: DuckyBot, owner_id: int, media_type: str, media: dict[str, Any]):
        super().__init__(timeout=600)
        self.bot = bot
        self.owner_id = owner_id
        self.media_type = media_type
        self.media = media

        status = (media.get("mediaInfo") or {}).get("status", 1)
        self.seasons = requestable_seasons(media) if media_type == "tv" else []
        if media_type == "movie":
            blocked = status in (2, 3, 4, 5)  # onbekend (1) en verwijderd (6) mogen opnieuw
        else:
            blocked = not self.seasons
        if blocked:
            self.request_button.disabled = True
            self.request_button.label = "Al beschikbaar" if status == STATUS_AVAILABLE else "Al aangevraagd"
            self.request_button.style = discord.ButtonStyle.secondary
        elif media_type == "tv" and status in (2, 3, 4, 5):
            self.request_button.label = f"Ontbrekende seizoenen aanvragen ({len(self.seasons)})"

        if link := jellyfin_link(bot, media):
            self.add_item(discord.ui.Button(label="Kijk in Jellyfin", url=link))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Dit is niet jouw aanvraag 🙂", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Aanvragen", style=discord.ButtonStyle.success, emoji="📥")
    async def request_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = await self.bot.resolve_seerr_user(interaction.user)
        if user is None:
            await interaction.followup.send(not_linked_message(self.bot), ephemeral=True)
            return
        try:
            result = await self.bot.seerr.create_request(user["id"], self.media_type, self.media["id"], self.seasons)
        except JellyseerrError as exc:
            log.warning("Aanvraag mislukt voor %s: %s", interaction.user, exc)
            await interaction.followup.send(f"❌ Aanvragen mislukt: {exc.message}", ephemeral=True)
            return

        button.disabled = True
        button.label = "Aangevraagd"
        button.style = discord.ButtonStyle.secondary
        await interaction.edit_original_response(view=self)

        title = title_of(self.media)
        if result.get("status") == 2:
            text = f"✅ **{title}** is aangevraagd en direct goedgekeurd. Je krijgt een DM zodra het beschikbaar is."
        else:
            text = f"📨 **{title}** is aangevraagd en wacht op goedkeuring van een admin."
        await interaction.followup.send(text, ephemeral=True)


class ResultSelect(discord.ui.Select):
    def __init__(self, results: list[dict[str, Any]]):
        options = []
        for r in results[:25]:
            year = year_of(r)
            label = f"{title_of(r)} ({year})" if year else title_of(r)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=f"{r['mediaType']}:{r['id']}",
                    emoji="🎬" if r["mediaType"] == "movie" else "📺",
                    description=(r.get("overview") or "")[:100] or None,
                )
            )
        super().__init__(placeholder="Kies de juiste titel", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: Media = interaction.client.get_cog("Media")  # type: ignore[assignment]
        media_type, tmdb_id = self.values[0].split(":")
        await cog.show_media(interaction, media_type, int(tmdb_id), edit=True)


class Media(commands.Cog):
    def __init__(self, bot: DuckyBot):
        self.bot = bot

    async def show_media(
        self, interaction: discord.Interaction, media_type: str, tmdb_id: int, *, edit: bool = False
    ) -> None:
        if edit:
            await interaction.response.defer()
        try:
            media = await self.bot.seerr.media(media_type, tmdb_id)
        except JellyseerrError as exc:
            await interaction.followup.send(f"❌ Kon de titel niet ophalen: {exc.message}", ephemeral=True)
            return
        embed = media_embed(self.bot, media_type, media)
        view = RequestView(self.bot, interaction.user.id, media_type, media)
        if edit:
            await interaction.edit_original_response(content=None, embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # --- /aanvraag -------------------------------------------------------

    @app_commands.command(name="aanvraag", description="Vraag een film of serie aan via Jellyseerr")
    @app_commands.describe(titel="Begin te typen en kies een titel uit de lijst")
    async def aanvraag(self, interaction: discord.Interaction, titel: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if match := CHOICE_RE.match(titel):
            await self.show_media(interaction, match[1], int(match[2]))
            return

        try:
            results = await self.bot.seerr.search(titel)
        except JellyseerrError as exc:
            await interaction.followup.send(f"❌ Zoeken mislukt: {exc.message}", ephemeral=True)
            return
        if not results:
            await interaction.followup.send(f"Niets gevonden voor **{titel}**.", ephemeral=True)
        elif len(results) == 1:
            await self.show_media(interaction, results[0]["mediaType"], results[0]["id"])
        else:
            view = discord.ui.View(timeout=300)
            view.add_item(ResultSelect(results))
            await interaction.followup.send(f"Resultaten voor **{titel}**:", view=view, ephemeral=True)

    @aanvraag.autocomplete("titel")
    async def aanvraag_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if len(current.strip()) < 2:
            return []
        try:
            results = await asyncio.wait_for(self.bot.seerr.search(current), timeout=2.5)
        except (JellyseerrError, asyncio.TimeoutError):
            return []
        choices = []
        for r in results[:25]:
            year = year_of(r)
            icon = "🎬" if r["mediaType"] == "movie" else "📺"
            name = f"{icon} {title_of(r)}" + (f" ({year})" if year else "")
            if (r.get("mediaInfo") or {}).get("status") == STATUS_AVAILABLE:
                name += " ✅"
            choices.append(app_commands.Choice(name=name[:100], value=f"{r['mediaType']}:{r['id']}"))
        return choices

    # --- /mijn-aanvragen -------------------------------------------------

    @app_commands.command(name="mijn-aanvragen", description="Bekijk je laatste aanvragen")
    async def mijn_aanvragen(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = await self.bot.resolve_seerr_user(interaction.user)
        if user is None:
            await interaction.followup.send(not_linked_message(self.bot), ephemeral=True)
            return
        try:
            requests = await self.bot.seerr.user_requests(user["id"], take=10)
        except JellyseerrError as exc:
            await interaction.followup.send(f"❌ Ophalen mislukt: {exc.message}", ephemeral=True)
            return
        if not requests:
            await interaction.followup.send("Je hebt nog niets aangevraagd. Probeer `/aanvraag`!", ephemeral=True)
            return

        async def describe(req: dict[str, Any]) -> str:
            media = req.get("media") or {}
            media_type = media.get("mediaType") or req.get("type", "movie")
            try:
                details = await self.bot.seerr.media(media_type, media["tmdbId"])
                title = title_of(details)
            except (JellyseerrError, KeyError):
                title = f"TMDB {media.get('tmdbId', '?')}"
            if req.get("status") == 2:
                status = MEDIA_STATUS.get(media.get("status", 1), "Goedgekeurd")
            else:
                status = REQUEST_STATUS.get(req.get("status"), "?")
            icon = "🎬" if media_type == "movie" else "📺"
            return f"{icon} **{title}** — {status}"

        lines = await asyncio.gather(*(describe(r) for r in requests))
        embed = discord.Embed(title="Je laatste aanvragen", description="\n".join(lines), color=COLOR_BRAND)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- koppelen --------------------------------------------------------

    @app_commands.command(name="koppeling", description="Laat zien aan welk Jellyseerr-account je gekoppeld bent")
    async def koppeling(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = await self.bot.resolve_seerr_user(interaction.user)
        if user is None:
            await interaction.followup.send(not_linked_message(self.bot), ephemeral=True)
        else:
            await interaction.followup.send(f"🔗 Gekoppeld aan Jellyseerr-account **{seerr_name(user)}**.", ephemeral=True)

    @app_commands.command(name="koppel", description="(Admin) Koppel een Discord-lid aan een Jellyseerr-account")
    @app_commands.describe(lid="Het Discord-lid", gebruiker="Het Jellyseerr-account")
    @app_commands.default_permissions(manage_guild=True)
    async def koppel(self, interaction: discord.Interaction, lid: discord.Member, gebruiker: str) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen dit.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            user = await self.bot.seerr.user(int(gebruiker))
            await self.bot.seerr.set_discord_id(user["id"], str(lid.id))
        except ValueError:
            await interaction.followup.send("Kies een gebruiker uit de lijst.", ephemeral=True)
            return
        except JellyseerrError as exc:
            await interaction.followup.send(f"❌ Koppelen mislukt: {exc.message}", ephemeral=True)
            return
        self.bot.links.set(lid.id, user["id"], seerr_name(user))
        await interaction.followup.send(
            f"🔗 {lid.mention} is gekoppeld aan **{seerr_name(user)}**.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @koppel.autocomplete("gebruiker")
    async def koppel_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            users = await asyncio.wait_for(self.bot.seerr.users(current or None, take=25), timeout=2.5)
        except (JellyseerrError, asyncio.TimeoutError):
            return []
        return [app_commands.Choice(name=seerr_name(u)[:100], value=str(u["id"])) for u in users[:25]]

    @app_commands.command(name="ontkoppel", description="(Admin) Verwijder de koppeling van een Discord-lid")
    @app_commands.default_permissions(manage_guild=True)
    async def ontkoppel(self, interaction: discord.Interaction, lid: discord.Member) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen dit.", ephemeral=True)
            return
        removed = self.bot.links.remove(lid.id)
        text = f"Koppeling van {lid.mention} verwijderd." if removed else f"{lid.mention} had geen lokale koppeling."
        text += "\n-# Staat het Discord-ID ook in het Jellyseerr-profiel, haal het daar ook weg."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: DuckyBot) -> None:
    await bot.add_cog(Media(bot))
