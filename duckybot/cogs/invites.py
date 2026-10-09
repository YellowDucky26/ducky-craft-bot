"""Uitnodigingen: Discord-invitelinks en Jellyfin-accounts."""
import logging
import re
import secrets

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import COLOR_BRAND, COLOR_INFO, DuckyBot, seerr_name
from ..jellyfin import JellyfinError
from ..jellyseerr import JellyseerrError

log = logging.getLogger(__name__)

DURATIONS = {
    "1 uur": 3600,
    "1 dag": 86400,
    "7 dagen": 604800,
    "Nooit": 0,
}


def username_for(member: discord.abc.User) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "", member.name)[:32]
    return name or f"user{member.id}"


class Invites(commands.GroupCog, group_name="invite", group_description="Nodig iemand uit"):
    def __init__(self, bot: DuckyBot):
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.bot.can_invite(interaction.user):
            return True
        await interaction.response.send_message("Je hebt geen rechten om uitnodigingen te maken.", ephemeral=True)
        return False

    async def log_admin(self, text: str) -> None:
        await self.bot.send_to(
            self.bot.config.admin_channel_id,
            embed=discord.Embed(description=text, color=COLOR_INFO),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- /invite discord -------------------------------------------------

    @app_commands.command(name="discord", description="Maak een uitnodigingslink voor deze Discord-server")
    @app_commands.describe(max_gebruik="Hoe vaak de link gebruikt mag worden (0 = onbeperkt)", geldig="Hoe lang de link geldig is")
    @app_commands.choices(geldig=[app_commands.Choice(name=k, value=k) for k in DURATIONS])
    async def discord_invite(
        self,
        interaction: discord.Interaction,
        max_gebruik: app_commands.Range[int, 0, 100] = 1,
        geldig: str = "1 dag",
    ) -> None:
        channel = await self.bot.channel(self.bot.config.invite_channel_id) or interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
            await interaction.response.send_message("Geen geschikt kanaal voor een invite gevonden.", ephemeral=True)
            return
        try:
            invite = await channel.create_invite(
                max_uses=max_gebruik,
                max_age=DURATIONS.get(geldig, 86400),
                unique=True,
                reason=f"Aangemaakt door {interaction.user} via /invite discord",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "De bot mist het recht *Uitnodiging maken* in dat kanaal.", ephemeral=True
            )
            return

        uses = "onbeperkt" if max_gebruik == 0 else f"{max_gebruik}×"
        await interaction.response.send_message(
            f"🎟️ Je uitnodiging (te gebruiken: {uses}, geldig: {geldig.lower()}):\n{invite.url}", ephemeral=True
        )
        await self.log_admin(f"🎟️ {interaction.user.mention} maakte een Discord-invite ({uses}, {geldig.lower()}).")

    # --- /invite jellyfin ------------------------------------------------

    @app_commands.command(name="jellyfin", description="Maak een Jellyfin-account aan voor een lid")
    @app_commands.describe(lid="Voor wie is het account?", gebruikersnaam="Optioneel; standaard de Discord-naam")
    async def jellyfin_invite(
        self, interaction: discord.Interaction, lid: discord.Member, gebruikersnaam: str | None = None
    ) -> None:
        jellyfin = self.bot.jellyfin
        if jellyfin is None:
            await interaction.response.send_message("Jellyfin is niet geconfigureerd voor deze bot.", ephemeral=True)
            return
        if lid.bot:
            await interaction.response.send_message("Bots krijgen geen Jellyfin-account 🤖", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        if self.bot.links.get(lid.id):
            await interaction.followup.send(
                f"{lid.mention} is al gekoppeld aan een account. Gebruik `/ontkoppel` als dat niet klopt.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        name = gebruikersnaam.strip() if gebruikersnaam else username_for(lid)
        password = secrets.token_urlsafe(9)
        try:
            if await jellyfin.user_exists(name):
                await interaction.followup.send(
                    f"Er bestaat al een Jellyfin-gebruiker **{name}**. Kies een andere `gebruikersnaam`.", ephemeral=True
                )
                return
            jf_user = await jellyfin.create_user(name, password)
        except JellyfinError as exc:
            log.warning("Jellyfin-account maken mislukt: %s", exc)
            await interaction.followup.send(f"❌ Account aanmaken in Jellyfin mislukt: {exc}", ephemeral=True)
            return

        warnings: list[str] = []
        try:
            seerr_user = await self.bot.seerr.import_jellyfin_user(jf_user["Id"])
            if seerr_user:
                await self.bot.seerr.set_discord_id(seerr_user["id"], str(lid.id))
                self.bot.links.set(lid.id, seerr_user["id"], seerr_name(seerr_user))
            else:
                warnings.append("Het account kon niet in Jellyseerr worden gevonden na importeren.")
        except JellyseerrError as exc:
            log.warning("Importeren in Jellyseerr mislukt: %s", exc)
            warnings.append(f"Importeren in Jellyseerr mislukt ({exc.message}); doe dit handmatig.")

        if self.bot.config.member_role_id:
            role = interaction.guild.get_role(self.bot.config.member_role_id) if interaction.guild else None
            try:
                if role:
                    await lid.add_roles(role, reason=f"Jellyfin-account aangemaakt door {interaction.user}")
            except discord.Forbidden:
                warnings.append("De bot mag de ledenrol niet toekennen (rol staat te hoog of recht ontbreekt).")

        cfg = self.bot.config
        welcome = discord.Embed(
            title="🦆 Welkom bij Ducky-Craft media!",
            description=(
                f"Er is een Jellyfin-account voor je aangemaakt door {interaction.user.mention}.\n"
                "Wijzig je wachtwoord na het eerste inloggen."
            ),
            color=COLOR_BRAND,
        )
        welcome.add_field(name="Gebruikersnaam", value=f"`{name}`")
        welcome.add_field(name="Wachtwoord", value=f"||`{password}`||")
        welcome.add_field(name="Kijken", value=cfg.jellyfin_public_url or "-", inline=False)
        welcome.add_field(
            name="Iets aanvragen",
            value=f"Gebruik `/aanvraag` in de server, of log met je Jellyfin-account in op {cfg.jellyseerr_public_url}",
            inline=False,
        )
        delivered = await self.bot.dm(lid.id, embed=welcome)

        lines = [f"✅ Jellyfin-account **{name}** aangemaakt voor {lid.mention}."]
        if delivered:
            lines.append("De inloggegevens zijn per DM verstuurd.")
        else:
            lines.append(
                f"⚠️ {lid.mention} accepteert geen DM's. Geef deze gegevens zelf door:\n"
                f"Gebruikersnaam: `{name}`\nWachtwoord: ||`{password}`||"
            )
        lines += [f"⚠️ {w}" for w in warnings]
        await interaction.followup.send("\n".join(lines), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await self.log_admin(f"👤 {interaction.user.mention} maakte Jellyfin-account **{name}** voor {lid.mention}.")


async def setup(bot: DuckyBot) -> None:
    await bot.add_cog(Invites(bot))
