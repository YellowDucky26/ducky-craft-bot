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
    ISSUE_TYPES,
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
IN_LIBRARY = (4, 5)  # deels of helemaal beschikbaar


def result_label(result: dict[str, Any], *, with_date: bool = False) -> str:
    """'🎬 Dune (2021) ✅' voor lijsten en autocomplete."""
    icon = "🎬" if result.get("mediaType") == "movie" else "📺"
    date = result.get("releaseDate") or result.get("firstAirDate") or ""
    if with_date and len(date) == 10:
        when = f" · {date[8:10]}-{date[5:7]}-{date[:4]}"
    else:
        when = f" ({date[:4]})" if date else ""
    label = f"{icon} {title_of(result)}{when}"
    if (result.get("mediaInfo") or {}).get("status") == STATUS_AVAILABLE:
        label += " ✅"
    return label


def quota_text(quota: dict[str, Any]) -> str:
    lines = []
    for key, label in (("movie", "Films"), ("tv", "Series")):
        status = quota.get(key) or {}
        if not status.get("limit"):
            lines.append(f"{label}: onbeperkt")
        else:
            lines.append(f"{label}: nog {status.get('remaining', 0)} van {status['limit']} (per {status.get('days')} dagen)")
    return "\n".join(lines)


def not_linked_message(bot: DuckyBot) -> str:
    url = bot.config.jellyseerr_public_url
    return (
        "Je Discord-account is nog niet gekoppeld aan een Jellyseerr-account.\n"
        f"Log in op {url}, ga naar **Profiel → Instellingen → Notificaties → Discord** en vul je "
        "Discord-gebruikers-ID in (rechtsklik op jezelf → *Gebruikers-ID kopiëren*). "
        "Of vraag een admin om `/link`."
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
        self.selected = list(self.seasons)
        self.partly_there = status in (2, 3, 4, 5)
        if blocked:
            self.request_button.disabled = True
            self.request_button.label = "Al beschikbaar" if status == STATUS_AVAILABLE else "Al aangevraagd"
            self.request_button.style = discord.ButtonStyle.secondary
        elif media_type == "tv":
            if 1 < len(self.seasons) <= 25:
                self.add_item(SeasonSelect(self.seasons))
            self.update_label()

        if link := jellyfin_link(bot, media):
            self.add_item(discord.ui.Button(label="Kijk in Jellyfin", url=link))

    def update_label(self) -> None:
        if len(self.selected) == 1:
            self.request_button.label = f"Seizoen {self.selected[0]} aanvragen"
        elif self.selected == self.seasons and self.partly_there:
            self.request_button.label = f"Ontbrekende seizoenen aanvragen ({len(self.selected)})"
        else:
            self.request_button.label = f"{len(self.selected)} seizoenen aanvragen"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Dit is niet jouw aanvraag 🙂", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Aanvragen", style=discord.ButtonStyle.success, emoji="📥", row=1)
    async def request_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = await self.bot.resolve_seerr_user(interaction.user)
        if user is None:
            await interaction.followup.send(not_linked_message(self.bot), ephemeral=True)
            return
        try:
            result = await self.bot.seerr.create_request(user["id"], self.media_type, self.media["id"], self.selected)
        except JellyseerrError as exc:
            log.warning("Aanvraag mislukt voor %s: %s", interaction.user, exc)
            await interaction.followup.send(f"❌ Aanvragen mislukt: {exc.message}", ephemeral=True)
            return

        for item in self.children:
            if isinstance(item, (discord.ui.Select, discord.ui.Button)) and not getattr(item, "url", None):
                item.disabled = True
        button.label = "Aangevraagd"
        button.style = discord.ButtonStyle.secondary
        await interaction.edit_original_response(view=self)

        title = title_of(self.media)
        if result.get("status") == 2:
            text = f"✅ **{title}** is aangevraagd en direct goedgekeurd. Je krijgt een DM zodra het beschikbaar is."
        else:
            text = f"📨 **{title}** is aangevraagd en wacht op goedkeuring van een admin."
        await interaction.followup.send(text, ephemeral=True)


class SeasonSelect(discord.ui.Select):
    """Welke seizoenen aanvragen; standaard alle die nog ontbreken."""

    def __init__(self, seasons: list[int]):
        options = [discord.SelectOption(label=f"Seizoen {n}", value=str(n), default=True) for n in seasons]
        super().__init__(placeholder="Kies seizoenen", min_values=1, max_values=len(options), options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RequestView = self.view  # type: ignore[assignment]
        view.selected = sorted(int(v) for v in self.values)
        for option in self.options:
            option.default = option.value in self.values
        view.update_label()
        await interaction.response.edit_message(view=view)


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


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailModal(discord.ui.Modal, title="E-mailadres voor Jellyseerr"):
    email = discord.ui.TextInput(label="E-mailadres", placeholder="naam@voorbeeld.nl", max_length=254)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: DuckyBot = interaction.client  # type: ignore[assignment]
        email = self.email.value.strip()
        if not EMAIL_RE.match(email):
            await interaction.response.send_message(f"**{email}** is geen geldig e-mailadres.", ephemeral=True)
            return
        link = bot.links.get(interaction.user.id)
        if link is None:
            await interaction.response.send_message(not_linked_message(bot), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await bot.seerr.set_email(link["seerr_id"], email)
        except JellyseerrError as exc:
            # Jellyseerr geeft 400 (InvalidEmail) als een ander account dit adres al heeft.
            reason = "dit e-mailadres hoort al bij een ander account." if exc.status == 400 else exc.message
            await interaction.followup.send(f"❌ Opslaan mislukt: {reason}", ephemeral=True)
            return
        await interaction.followup.send(f"📧 E-mailadres **{email}** opgeslagen in Jellyseerr.", ephemeral=True)


class EmailButton(discord.ui.DynamicItem[discord.ui.Button], template=r"seerr:email"):
    """Opent het e-mailvenster; blijft werken na een herstart. Geldt altijd voor wie klikt."""

    def __init__(self):
        super().__init__(
            discord.ui.Button(label="E-mailadres invullen", emoji="📧", style=discord.ButtonStyle.primary, custom_id="seerr:email")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: DuckyBot = interaction.client  # type: ignore[assignment]
        if bot.links.get(interaction.user.id) is None:
            await interaction.response.send_message(not_linked_message(bot), ephemeral=True)
            return
        await interaction.response.send_modal(EmailModal())


def email_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(EmailButton())
    return view


class Media(commands.Cog):
    def __init__(self, bot: DuckyBot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(EmailButton)

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

    # --- /request --------------------------------------------------------

    @app_commands.command(name="request", description="Request a movie or series through Jellyseerr")
    @app_commands.rename(titel="title")
    @app_commands.describe(titel="Start typing and pick a title from the list")
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

    async def title_choices(self, current: str, *, only_in_library: bool = False) -> list[app_commands.Choice[str]]:
        if len(current.strip()) < 2:
            return []
        try:
            results = await asyncio.wait_for(self.bot.seerr.search(current), timeout=2.5)
        except (JellyseerrError, asyncio.TimeoutError):
            return []
        if only_in_library:
            results = [r for r in results if (r.get("mediaInfo") or {}).get("status") in IN_LIBRARY]
        return [
            app_commands.Choice(name=result_label(r)[:100], value=f"{r['mediaType']}:{r['id']}") for r in results[:25]
        ]

    @aanvraag.autocomplete("titel")
    async def aanvraag_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.title_choices(current)

    # --- /trending en /upcoming -------------------------------------------

    async def show_list(self, interaction: discord.Interaction, heading: str, fetch, *, with_date: bool = False) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await fetch()
        except JellyseerrError as exc:
            await interaction.followup.send(f"❌ Ophalen mislukt: {exc.message}", ephemeral=True)
            return
        if not results:
            await interaction.followup.send("Niets gevonden.", ephemeral=True)
            return
        lines = [f"`{i:>2}.` {result_label(r, with_date=with_date)}" for i, r in enumerate(results[:15], 1)]
        embed = discord.Embed(title=heading, description="\n".join(lines), color=COLOR_BRAND)
        embed.set_footer(text="Kies hieronder een titel om hem te bekijken of aan te vragen.")
        view = discord.ui.View(timeout=300)
        view.add_item(ResultSelect(results[:25]))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="trending", description="Popular movies and series this week")
    @app_commands.rename(soort="type")
    @app_commands.describe(soort="Movies, series or both")
    @app_commands.choices(soort=[
        app_commands.Choice(name="Movies and series", value="all"),
        app_commands.Choice(name="Movies", value="movie"),
        app_commands.Choice(name="Series", value="tv"),
    ])
    async def trending(self, interaction: discord.Interaction, soort: str = "all") -> None:
        await self.show_list(interaction, "🔥 Populair deze week", lambda: self.bot.seerr.trending(soort))

    @app_commands.command(name="upcoming", description="Movies and series coming out soon")
    @app_commands.rename(soort="type")
    @app_commands.describe(soort="Movies or series")
    @app_commands.choices(soort=[
        app_commands.Choice(name="Movies", value="movie"),
        app_commands.Choice(name="Series", value="tv"),
    ])
    async def upcoming(self, interaction: discord.Interaction, soort: str = "movie") -> None:
        heading = "📅 Binnenkort in de bioscoop" if soort == "movie" else "📅 Binnenkort op tv"
        await self.show_list(interaction, heading, lambda: self.bot.seerr.upcoming(soort), with_date=True)

    # --- /issue ----------------------------------------------------------

    @app_commands.command(name="issue", description="Report a problem with a movie or episode")
    @app_commands.rename(titel="title", soort="type", omschrijving="description", seizoen="season", aflevering="episode")
    @app_commands.describe(
        titel="Start typing and pick a title from the list",
        soort="What is wrong?",
        omschrijving="Describe the problem",
        seizoen="Series only: which season",
        aflevering="Series only: which episode",
    )
    @app_commands.choices(soort=[
        app_commands.Choice(name="Video", value=1),
        app_commands.Choice(name="Audio", value=2),
        app_commands.Choice(name="Subtitles", value=3),
        app_commands.Choice(name="Other", value=4),
    ])
    async def issue(
        self,
        interaction: discord.Interaction,
        titel: str,
        soort: int,
        omschrijving: app_commands.Range[str, 3, 1000],
        seizoen: app_commands.Range[int, 1, 100] | None = None,
        aflevering: app_commands.Range[int, 1, 1000] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        match = CHOICE_RE.match(titel)
        if match is None:
            await interaction.followup.send("Kies een titel uit de lijst die verschijnt tijdens het typen.", ephemeral=True)
            return
        user = await self.bot.resolve_seerr_user(interaction.user)
        if user is None:
            await interaction.followup.send(not_linked_message(self.bot), ephemeral=True)
            return
        media_type = match[1]
        try:
            media = await self.bot.seerr.media(media_type, int(match[2]))
            media_id = (media.get("mediaInfo") or {}).get("id")
            if not media_id or media["mediaInfo"].get("status") not in IN_LIBRARY:
                await interaction.followup.send(
                    f"**{title_of(media)}** staat niet in Jellyfin; je kunt alleen problemen melden bij titels die er staan.",
                    ephemeral=True,
                )
                return
            issue = await self.bot.seerr.create_issue(
                user["id"], media_id, soort, omschrijving,
                season=seizoen if media_type == "tv" else None,
                episode=aflevering if media_type == "tv" else None,
            )
        except JellyseerrError as exc:
            await interaction.followup.send(f"❌ Melden mislukt: {exc.message}", ephemeral=True)
            return
        where = ""
        if media_type == "tv" and seizoen:
            where = f" (seizoen {seizoen}" + (f", aflevering {aflevering})" if aflevering else ")")
        url = f"{self.bot.config.jellyseerr_public_url}/issues/{issue['id']}"
        await interaction.followup.send(
            f"🐞 Probleem ({ISSUE_TYPES[soort].lower()}) gemeld bij **{title_of(media)}**{where}.\n"
            f"Je krijgt een DM zodra het is opgelost. [Bekijk de melding]({url})",
            ephemeral=True,
        )

    @issue.autocomplete("titel")
    async def issue_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.title_choices(current, only_in_library=True)

    # --- /my-requests ----------------------------------------------------

    @app_commands.command(name="my-requests", description="Show your latest requests")
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
            await interaction.followup.send("Je hebt nog niets aangevraagd. Probeer `/request`!", ephemeral=True)
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
        try:
            embed.add_field(name="Je limiet", value=quota_text(await self.bot.seerr.quota(user["id"])), inline=False)
        except JellyseerrError as exc:
            log.info("Quota ophalen mislukt: %s", exc)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- koppelen --------------------------------------------------------

    @app_commands.command(name="my-account", description="Show which Jellyseerr account you are linked to")
    async def koppeling(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        user = await self.bot.resolve_seerr_user(interaction.user)
        if user is None:
            await interaction.followup.send(not_linked_message(self.bot), ephemeral=True)
        else:
            await interaction.followup.send(
                f"🔗 Gekoppeld aan Jellyseerr-account **{seerr_name(user)}**.", view=email_view(), ephemeral=True
            )

    @app_commands.command(name="link", description="(Admin) Link a Discord member to a Jellyseerr account")
    @app_commands.rename(lid="member", gebruiker="user")
    @app_commands.describe(lid="The Discord member", gebruiker="The Jellyseerr account")
    @app_commands.default_permissions(manage_guild=True)
    async def koppel(self, interaction: discord.Interaction, lid: discord.Member, gebruiker: str) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen dit.", ephemeral=True)
            return
        if lid.bot:
            await interaction.response.send_message("Bots kun je niet koppelen 🤖 Kies het Discord-lid zelf.", ephemeral=True)
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
        delivered = await self.bot.dm(
            lid.id,
            embed=discord.Embed(
                title="🔗 Je bent gekoppeld aan Jellyseerr",
                description=(
                    f"Je Discord-account is gekoppeld aan het Jellyseerr-account **{seerr_name(user)}**.\n"
                    "Vul je e-mailadres in, dan staat het in je Jellyseerr-profiel."
                ),
                color=COLOR_BRAND,
            ),
            view=email_view(),
        )
        text = f"🔗 {lid.mention} is gekoppeld aan **{seerr_name(user)}**."
        if delivered:
            text += "\nHet lid heeft een DM gekregen om het e-mailadres in te vullen."
        else:
            text += f"\n⚠️ {lid.mention} accepteert geen DM's; het e-mailadres kan ook via `/my-account`."
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @koppel.autocomplete("gebruiker")
    async def koppel_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            users = await asyncio.wait_for(self.bot.seerr.users(current or None, take=25), timeout=2.5)
        except (JellyseerrError, asyncio.TimeoutError):
            return []
        return [app_commands.Choice(name=seerr_name(u)[:100], value=str(u["id"])) for u in users[:25]]

    @app_commands.command(name="unlink", description="(Admin) Remove the link of a Discord member")
    @app_commands.rename(lid="member")
    @app_commands.describe(lid="The Discord member")
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
