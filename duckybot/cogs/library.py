"""De Jellyfin-bibliotheek: wie kijkt er nu, wat is er nieuw, en het geplande overzicht."""
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..bot import BOT_NAME, COLOR_BRAND, DuckyBot
from ..jellyfin import JellyfinError, details_url, image_url

log = logging.getLogger(__name__)

MAX_EMBEDS = 10  # limiet van Discord per bericht


# --- zonder Discord-I/O, zodat het testbaar is ------------------------------

def episode_code(item: dict[str, Any]) -> str:
    return f"S{item.get('ParentIndexNumber') or 0:02}E{item.get('IndexNumber') or 0:02}"


def describe_session(session: dict[str, Any]) -> str:
    item = session["NowPlayingItem"]
    if item.get("Type") == "Episode":
        title = f"{item.get('SeriesName', '?')} {episode_code(item)} · {item.get('Name', '')}"
    elif item.get("ProductionYear"):
        title = f"{item.get('Name', '?')} ({item['ProductionYear']})"
    else:
        title = item.get("Name", "?")
    state = session.get("PlayState") or {}
    icon = "⏸️" if state.get("IsPaused") else "▶️"
    line = f"{icon} **{session.get('UserName', '?')}** — {title}"
    runtime = item.get("RunTimeTicks") or 0
    if runtime:
        line += f" · {min(100, round(100 * (state.get('PositionTicks') or 0) / runtime))}%"
    device = " op ".join(part for part in (session.get("Client"), session.get("DeviceName")) if part)
    if device:
        line += f"\n-# {device}"
    return line


@dataclass
class NewTitle:
    title: str
    detail: str
    item_id: str        # voor link en poster: de film zelf, of de serie
    overview: str = ""


def episodes_detail(episodes: list[dict[str, Any]]) -> str:
    episodes = sorted(episodes, key=lambda e: (e.get("ParentIndexNumber") or 0, e.get("IndexNumber") or 0))
    if len(episodes) == 1:
        return f"📺 Nieuwe aflevering: {episode_code(episodes[0])} · {episodes[0].get('Name', '')}"
    if len(episodes) <= 3:
        return f"📺 {len(episodes)} nieuwe afleveringen: " + ", ".join(episode_code(e) for e in episodes)
    return f"📺 {len(episodes)} nieuwe afleveringen: {episode_code(episodes[0])} t/m {episode_code(episodes[-1])}"


def group_new_items(items: list[dict[str, Any]]) -> list[NewTitle]:
    """Films los, afleveringen samengevoegd per serie; in de volgorde waarin ze binnenkwamen."""
    titles: list[NewTitle] = []
    series: dict[str, tuple[NewTitle, list[dict[str, Any]]]] = {}
    for item in items:
        if item.get("Type") != "Episode":
            year = item.get("ProductionYear")
            titles.append(NewTitle(item.get("Name", "?"), f"🎬 Film{f' · {year}' if year else ''}", item["Id"],
                                   item.get("Overview") or ""))
            continue
        series_id = item.get("SeriesId") or item["Id"]
        if series_id not in series:
            series[series_id] = (NewTitle(item.get("SeriesName") or "Onbekende serie", "", series_id), [])
            titles.append(series[series_id][0])
        series[series_id][1].append(item)
    for entry, episodes in series.values():
        entry.detail = episodes_detail(episodes)
    return titles


def digest_due(now: datetime, mode: str, weekday: int) -> bool:
    return mode == "daily" or (mode == "weekly" and now.weekday() == weekday)


def report_time(cfg) -> time:
    hour, minute = (int(part) for part in cfg.report_time.split(":"))
    return time(hour, minute, tzinfo=cfg.tz)


# --- de cog ------------------------------------------------------------------

class Library(commands.Cog):
    def __init__(self, bot: DuckyBot):
        self.bot = bot

    async def cog_load(self) -> None:
        if self.bot.jellyfin and self.bot.config.digest != "off":
            self.daily_digest.change_interval(time=report_time(self.bot.config))
            self.daily_digest.start()

    async def cog_unload(self) -> None:
        self.daily_digest.cancel()

    def new_titles_embeds(self, titles: list[NewTitle]) -> list[discord.Embed]:
        base = self.bot.config.jellyfin_public_url
        embeds = []
        for entry in titles[:MAX_EMBEDS]:
            overview = entry.overview if len(entry.overview) <= 200 else entry.overview[:197] + "..."
            embed = discord.Embed(
                title=entry.title[:256],
                description="\n".join(part for part in (entry.detail, overview) if part) or None,
                color=COLOR_BRAND,
                url=details_url(base, entry.item_id) if base else None,
            )
            if base:
                embed.set_thumbnail(url=image_url(base, entry.item_id))
            embeds.append(embed)
        return embeds

    async def new_titles_message(self, since: datetime) -> dict[str, Any] | None:
        """Berichtinhoud met de nieuwe titels sinds ``since``, of None als er niets nieuws is."""
        assert self.bot.jellyfin is not None
        titles = group_new_items(await self.bot.jellyfin.items_added_since(since))
        if not titles:
            return None
        local = since.astimezone(self.bot.config.tz)
        content = f"🆕 **Nieuw in Jellyfin** sinds {local:%d-%m-%Y}"
        if len(titles) > MAX_EMBEDS:
            content += f"\n-# …en nog {len(titles) - MAX_EMBEDS} andere titels"
        return {"content": content, "embeds": self.new_titles_embeds(titles)}

    # --- gepland overzicht ------------------------------------------------

    @tasks.loop(hours=24)  # het echte tijdstip wordt in cog_load gezet (REPORT_TIME)
    async def daily_digest(self) -> None:
        cfg = self.bot.config
        now = datetime.now(cfg.tz)
        if not digest_due(now, cfg.digest, cfg.digest_day):
            return
        period = timedelta(days=1 if cfg.digest == "daily" else 7)
        last = self.bot.state.get("digest_last")
        since = datetime.fromisoformat(last) if last else now - period
        try:
            message = await self.new_titles_message(since)
        except JellyfinError as exc:
            log.warning("Overzicht van nieuwe titels mislukt: %s", exc)
            return
        if message:
            channel_id = cfg.digest_channel_id or cfg.announce_channel_id or cfg.request_channel_id
            if await self.bot.send_to(channel_id, **message) is None:
                return  # volgende keer opnieuw proberen, met dezelfde begintijd
        self.bot.state.set("digest_last", now.isoformat())

    @daily_digest.before_loop
    async def before_digest(self) -> None:
        await self.bot.wait_until_ready()

    # --- commando's -------------------------------------------------------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.bot.jellyfin is None:
            await interaction.response.send_message("Jellyfin is niet geconfigureerd voor deze bot.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="now-playing", description="See who is watching what on Jellyfin right now")
    async def now_playing(self, interaction: discord.Interaction) -> None:
        assert self.bot.jellyfin is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            sessions = await self.bot.jellyfin.now_playing()
        except JellyfinError as exc:
            await interaction.followup.send(f"❌ Jellyfin niet bereikbaar: {exc}", ephemeral=True)
            return
        if not sessions:
            await interaction.followup.send(f"Er kijkt nu niemand; {BOT_NAME} doet even een dutje. 🦆💤", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Nu aan het kijken ({len(sessions)})",
            description="\n".join(describe_session(s) for s in sessions)[:4096],
            color=COLOR_BRAND,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="whats-new", description="See what was recently added to Jellyfin")
    @app_commands.rename(dagen="days")
    @app_commands.describe(dagen="How many days back (default 7)")
    async def whats_new(self, interaction: discord.Interaction, dagen: app_commands.Range[int, 1, 60] = 7) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        since = datetime.now(self.bot.config.tz) - timedelta(days=dagen)
        try:
            message = await self.new_titles_message(since)
        except JellyfinError as exc:
            await interaction.followup.send(f"❌ Jellyfin niet bereikbaar: {exc}", ephemeral=True)
            return
        if message is None:
            await interaction.followup.send(f"Niets nieuws in de afgelopen {dagen} dagen.", ephemeral=True)
            return
        await interaction.followup.send(**message, ephemeral=True)


async def setup(bot: DuckyBot) -> None:
    await bot.add_cog(Library(bot))
