"""Doorsturen: DM's aan de bot en pings van de bot gaan als DM naar één vaste persoon (FORWARD_USER_ID)."""
import logging

import discord
from discord.ext import commands

from ..bot import COLOR_BRAND, COLOR_INFO, BOT_NAME, DuckyBot

log = logging.getLogger(__name__)

# Bijlagen worden opnieuw geüpload (Discord-links naar bijlagen verlopen); daarboven alleen een link.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_FILES = 10


def forward_reason(message: discord.Message, me: discord.abc.User) -> str | None:
    """'dm' of 'ping' als dit bericht voor de bot bedoeld is, anders None."""
    if message.guild is None:
        return "dm"
    if me in message.mentions:
        return "ping"
    # @Kwakkel kan ook de beheerde rol van de bot zijn in plaats van de gebruiker.
    if message.guild.self_role and message.guild.self_role in message.role_mentions:
        return "ping"
    return None


def forward_embed(message: discord.Message, reason: str, uploaded: set[str]) -> discord.Embed:
    """Embed met de inhoud; ``uploaded`` zijn de bijlagen die als bestand meegaan."""
    author = message.author
    if reason == "dm":
        title = f"📨 DM aan {BOT_NAME}"
    else:
        title = f"🔔 Ping in #{getattr(message.channel, 'name', '?')}"
    embed = discord.Embed(
        title=title,
        description=(message.content or "*(geen tekst zichtbaar)*")[:4000],
        color=COLOR_INFO if reason == "dm" else COLOR_BRAND,
        timestamp=message.created_at,
    )
    embed.set_author(name=f"{author.display_name} (@{author.name})", icon_url=author.display_avatar.url)
    links = [f"[{a.filename}]({a.url})" for a in message.attachments if a.filename not in uploaded]
    if links:
        embed.add_field(name="Bijlagen (te groot om mee te sturen)", value="\n".join(links)[:1024], inline=False)
    if message.stickers:
        embed.add_field(name="Stickers", value=", ".join(s.name for s in message.stickers)[:1024], inline=False)
    embed.set_footer(text=f"ID {author.id}")
    return embed


class Inbox(commands.Cog):
    def __init__(self, bot: DuckyBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        target = self.bot.config.forward_user_id
        me = self.bot.user
        if not target or me is None or message.author.bot or message.author.id == target:
            return
        reason = forward_reason(message, me)
        if reason is None:
            return

        files: list[discord.File] = []
        total = 0
        for attachment in message.attachments[:MAX_FILES]:
            if total + attachment.size > MAX_UPLOAD_BYTES:
                continue
            try:
                files.append(await attachment.to_file())
                total += attachment.size
            except discord.HTTPException as exc:
                log.info("Bijlage %s ophalen mislukt: %s", attachment.filename, exc)

        view = None
        if message.guild is not None:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Naar het bericht", url=message.jump_url))
        delivered = await self.bot.dm(
            target,
            content=message.author.mention,
            embed=forward_embed(message, reason, {f.filename for f in files}),
            files=files,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if not delivered:
            log.warning("Doorsturen van %s van %s naar %s mislukt", reason, message.author, target)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        # De bot heeft alleen slash-commando's, maar een ping telt als prefix ("when_mentioned").
        # Zonder deze listener logt discord.py bij elke ping een CommandNotFound-fout.
        if not isinstance(error, commands.CommandNotFound):
            log.error("Fout in prefix-commando", exc_info=error)


async def setup(bot: DuckyBot) -> None:
    await bot.add_cog(Inbox(bot))
