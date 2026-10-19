"""/embed: een bericht met embeds uit een JSON-bestand plaatsen of bijwerken (Discohook-/webhookformaat)."""
import json
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import DuckyBot
from .notifications import parse_color

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 256 * 1024

# Discord-limieten voor één bericht
MAX_CONTENT = 2000
MAX_EMBEDS = 10
MAX_TOTAL = 6000
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELDS = 25
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048
MAX_AUTHOR = 256

# <#REGELS-ID> en dergelijke: een kanaalvermelding waar nog geen echt ID in staat.
CHANNEL_PLACEHOLDER = re.compile(r"<#(?!\d+>)[^>]*>")
MESSAGE_LINK = re.compile(r"/channels/(?:\d+|@me)/(\d+)/(\d+)")


class EmbedError(ValueError):
    """Ongeldig embed-bestand; de tekst is bedoeld voor de gebruiker."""


def _message_data(data: Any) -> dict[str, Any]:
    """Het bericht uit het bestand: webhook-JSON, Discohook-export, één embed of een lijst embeds."""
    if isinstance(data, list):
        return {"embeds": data}
    if not isinstance(data, dict):
        raise EmbedError("Het bestand bevat geen JSON-object.")
    if "messages" in data:  # Discohook "Backup"/deellink-export
        messages = data["messages"] or []
        if len(messages) != 1:
            raise EmbedError(f"Het bestand bevat {len(messages)} berichten; plaats ze één voor één.")
        data = messages[0].get("data", messages[0])
    if "embeds" in data or "content" in data:
        return data
    if any(key in data for key in ("title", "description", "fields")):
        return {"embeds": [data]}
    raise EmbedError("Geen `embeds` of `content` gevonden in het bestand.")


def _texts(embed: dict[str, Any]) -> list[str]:
    texts = [embed.get("title") or "", embed.get("description") or ""]
    texts += [(embed.get("footer") or {}).get("text") or "", (embed.get("author") or {}).get("name") or ""]
    for field in embed.get("fields") or []:
        texts += [field.get("name") or "", field.get("value") or ""]
    return texts


def _check_embed(index: int, embed: dict[str, Any]) -> None:
    label = f"Embed {index}"
    if not isinstance(embed, dict):
        raise EmbedError(f"{label} is geen JSON-object.")
    if len(embed.get("title") or "") > MAX_TITLE:
        raise EmbedError(f"{label}: de titel is langer dan {MAX_TITLE} tekens.")
    if len(embed.get("description") or "") > MAX_DESCRIPTION:
        raise EmbedError(f"{label}: de beschrijving is langer dan {MAX_DESCRIPTION} tekens.")
    if len((embed.get("footer") or {}).get("text") or "") > MAX_FOOTER:
        raise EmbedError(f"{label}: de footer is langer dan {MAX_FOOTER} tekens.")
    if len((embed.get("author") or {}).get("name") or "") > MAX_AUTHOR:
        raise EmbedError(f"{label}: de auteursnaam is langer dan {MAX_AUTHOR} tekens.")
    fields = embed.get("fields") or []
    if len(fields) > MAX_FIELDS:
        raise EmbedError(f"{label} heeft {len(fields)} velden; het maximum is {MAX_FIELDS}.")
    for field in fields:
        name, value = str(field.get("name") or ""), str(field.get("value") or "")
        if not name.strip() or not value.strip():
            raise EmbedError(f"{label}: elk veld heeft een naam en een waarde nodig.")
        if len(name) > MAX_FIELD_NAME:
            raise EmbedError(f"{label}: veldnaam “{name[:40]}…” is langer dan {MAX_FIELD_NAME} tekens.")
        if len(value) > MAX_FIELD_VALUE:
            raise EmbedError(f"{label}: veld “{name}” is {len(value)} tekens; het maximum is {MAX_FIELD_VALUE}.")
    if not any(text.strip() for text in _texts(embed)) and not embed.get("image") and not embed.get("thumbnail"):
        raise EmbedError(f"{label} is leeg.")


def parse_embed_file(raw: bytes) -> tuple[str | None, list[discord.Embed]]:
    """Lees en controleer een embed-bestand. Geeft (content, embeds) of gooit EmbedError."""
    if len(raw) > MAX_FILE_BYTES:
        raise EmbedError("Het bestand is te groot (maximaal 256 KB).")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise EmbedError("Het bestand is geen UTF-8-tekst.") from None
    except json.JSONDecodeError as exc:
        raise EmbedError(f"Ongeldige JSON op regel {exc.lineno}, kolom {exc.colno}: {exc.msg}.") from None

    message = _message_data(data)
    content = message.get("content") or None
    if content is not None:
        content = str(content)
        if len(content) > MAX_CONTENT:
            raise EmbedError(f"De berichttekst is langer dan {MAX_CONTENT} tekens.")

    raw_embeds = message.get("embeds") or []
    if not isinstance(raw_embeds, list):
        raise EmbedError("`embeds` moet een lijst zijn.")
    if len(raw_embeds) > MAX_EMBEDS:
        raise EmbedError(f"Het bestand bevat {len(raw_embeds)} embeds; het maximum is {MAX_EMBEDS}.")
    if not raw_embeds and not content:
        raise EmbedError("Het bericht is leeg: geen tekst en geen embeds.")

    texts = [content or ""]
    for index, embed in enumerate(raw_embeds, start=1):
        _check_embed(index, embed)
        texts += _texts(embed)
    for text in texts:
        if match := CHANNEL_PLACEHOLDER.search(text):
            raise EmbedError(f"`{match.group(0)}` is nog geen kanaal-ID. Vul het echte ID in, bv. `<#123456789012345678>`.")

    embeds = []
    for embed in raw_embeds:
        embed = dict(embed)
        if "color" in embed:
            embed["color"] = parse_color(embed["color"])
        embeds.append(discord.Embed.from_dict(embed))
    total = sum(len(embed) for embed in embeds)
    if total > MAX_TOTAL:
        raise EmbedError(f"Alle embeds samen zijn {total} tekens; Discord staat er {MAX_TOTAL} per bericht toe.")
    return content, embeds


def parse_message_ref(value: str) -> tuple[int | None, int]:
    """(kanaal-ID of None, bericht-ID) uit een berichtlink of een los bericht-ID."""
    value = value.strip()
    if match := MESSAGE_LINK.search(value):
        return int(match.group(1)), int(match.group(2))
    if value.isdigit():
        return None, int(value)
    raise EmbedError("Geef een berichtlink (rechtsklik op het bericht → *Link naar bericht kopiëren*) of een bericht-ID.")


class Embeds(commands.Cog):
    def __init__(self, bot: DuckyBot):
        self.bot = bot

    @app_commands.command(name="embed", description="(Admin) Post or update a message from a JSON embed file")
    @app_commands.describe(
        file="JSON in Discohook/webhook format: {\"content\": ..., \"embeds\": [...]}",
        channel="Where to post; defaults to this channel",
        ping="Optional role (or @everyone) to ping above the message",
        edit="Link to an earlier message from the bot to update instead of posting a new one",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def embed(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        channel: discord.TextChannel | None = None,
        ping: discord.Role | None = None,
        edit: str | None = None,
    ) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen embeds plaatsen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if file.size > MAX_FILE_BYTES:
                raise EmbedError("Het bestand is te groot (maximaal 256 KB).")
            content, embeds = parse_embed_file(await file.read())
            if edit:
                existing = await self._own_message(interaction, channel, edit)
            else:
                destination = channel or interaction.channel
                if destination is None:
                    raise EmbedError("Geen kanaal gevonden om naartoe te sturen.")
        except EmbedError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        # Mentions in het bestand pingen nooit; alleen de expliciete ping-optie doet dat.
        allowed = discord.AllowedMentions.none()
        if ping is not None:
            mention = "@everyone" if ping.is_default() else ping.mention
            content = f"{mention}\n{content}" if content else mention
            allowed = discord.AllowedMentions(everyone=True) if ping.is_default() else discord.AllowedMentions(roles=[ping])

        try:
            if edit:
                message = await existing.edit(content=content, embeds=embeds, allowed_mentions=allowed)
                done = "Bericht bijgewerkt"
            else:
                message = await destination.send(content=content, embeds=embeds, allowed_mentions=allowed)
                done = "Bericht geplaatst"
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ Discord weigerde het bericht: {exc.text}", ephemeral=True)
            return
        log.info("%s door %s: %s", done, interaction.user, message.jump_url)
        await interaction.followup.send(f"✅ {done}: {message.jump_url}", ephemeral=True)

    async def _own_message(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None, edit: str
    ) -> discord.Message:
        """Het bericht van de bot dat bijgewerkt moet worden (link of ID, dan in ``channel``/hier)."""
        channel_id, message_id = parse_message_ref(edit)
        source = await self.bot.channel(channel_id) if channel_id else (channel or interaction.channel)
        if source is None:
            raise EmbedError("Het kanaal van dat bericht is niet bereikbaar.")
        try:
            message = await source.fetch_message(message_id)  # type: ignore[union-attr]
        except discord.HTTPException:
            raise EmbedError("Dat bericht kon ik niet vinden.") from None
        if self.bot.user is None or message.author.id != self.bot.user.id:
            raise EmbedError("Ik kan alleen mijn eigen berichten bijwerken.")
        return message


async def setup(bot: DuckyBot) -> None:
    await bot.add_cog(Embeds(bot))
