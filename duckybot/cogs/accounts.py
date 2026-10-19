"""Accountbeheer: wachtwoord resetten, leden die vertrekken, inactieve accounts en statistieken."""
import asyncio
import logging
import re
import secrets
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..bot import BOT_NAME, COLOR_BRAND, COLOR_ERROR, COLOR_INFO, COLOR_WARN, DuckyBot, seerr_name
from ..jellyfin import JellyfinError, parse_date
from ..jellyseerr import JellyseerrError
from .library import report_time

log = logging.getLogger(__name__)


# --- zonder Discord-I/O, zodat het testbaar is ------------------------------

def find_inactive(users: list[dict[str, Any]], now: datetime, days: int) -> list[tuple[dict[str, Any], datetime | None]]:
    """Jellyfin-accounts zonder activiteit in de laatste ``days`` dagen; beheerders en uitgeschakelde tellen niet mee."""
    cutoff = now - timedelta(days=days)
    inactive = []
    for user in users:
        policy = user.get("Policy") or {}
        if policy.get("IsDisabled") or policy.get("IsAdministrator"):
            continue
        last = parse_date(user.get("LastActivityDate"))
        if last is None or last < cutoff:
            inactive.append((user, last))
    # Nooit actief eerst, daarna het langst geleden
    return sorted(inactive, key=lambda pair: pair[1] or datetime.min.replace(tzinfo=now.tzinfo))


def top(counter: Counter, n: int = 5) -> str:
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < len(medals) else '▫️'} **{name}** — {count}"
        for i, (name, count) in enumerate(counter.most_common(n))
        if count
    ]
    return "\n".join(lines) or "-"


def membership_length(joined: datetime, now: datetime) -> str:
    days = (now - joined).days
    if days < 1:
        return "minder dan een dag"
    if days < 31:
        return "1 dag" if days == 1 else f"{days} dagen"
    years, months = divmod(days * 12 // 365, 12)
    parts = []
    if years:
        parts.append(f"{years} jaar")
    if months:
        parts.append("1 maand" if months == 1 else f"{months} maanden")
    return " en ".join(parts)


REMOVALS = {"kick": ("👢", "is uit de server gezet"), "ban": ("🔨", "is verbannen")}


def leave_embed(
    member: Any, now: datetime, removal: tuple[str, str] | None, link: dict[str, Any] | None
) -> discord.Embed:
    """Melding voor een lid dat de server verlaat; ``removal`` is (kick/ban, toelichting) uit het auditlog."""
    emoji, verb = REMOVALS[removal[0]] if removal else ("👋", "heeft de server verlaten")
    embed = discord.Embed(
        title=f"{emoji} {member.display_name} {verb}",
        description=f"{member.mention} ({member.name}){' · bot' if member.bot else ''}",
        color=COLOR_ERROR if removal else COLOR_WARN,
        timestamp=now,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    if member.joined_at:
        since = discord.utils.format_dt(member.joined_at, "D")
        embed.add_field(name="Lid sinds", value=f"{since} ({membership_length(member.joined_at, now)})")
    roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
    if roles:
        embed.add_field(name="Rollen", value=" ".join(roles)[:1024], inline=False)
    if removal:
        embed.add_field(name="Door", value=removal[1], inline=False)
    if link:
        embed.add_field(name="Jellyseerr-account", value=link["name"])
    if member.guild.member_count:
        embed.set_footer(text=f"Nog {member.guild.member_count} leden · ID {member.id}")
    return embed


# --- vertrokken leden: knoppen in het adminkanaal ---------------------------

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, cog: "Accounts", discord_id: int, report: discord.Message | None):
        super().__init__(timeout=60)
        self.cog = cog
        self.discord_id = discord_id
        self.report = report

    @discord.ui.button(label="Ja, Jellyfin-account verwijderen", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="⏳ Bezig met verwijderen…", view=None)
        result = await self.cog.delete_account(self.discord_id)
        await interaction.edit_original_response(content=result)
        if result.startswith("✅") and self.report:
            await self.cog.close_report(self.report, f"Verwijderd door {interaction.user.mention}")


class DepartureButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"departed:(?P<action>disable|delete|ignore):(?P<id>\d+)",
):
    """Knoppen onder 'lid heeft de server verlaten'; blijven werken na een herstart."""

    STYLES = {
        "disable": ("Jellyfin-account uitschakelen", "⛔", discord.ButtonStyle.primary),
        "delete": ("Verwijderen", "🗑️", discord.ButtonStyle.danger),
        "ignore": ("Negeren", None, discord.ButtonStyle.secondary),
    }

    def __init__(self, action: str, discord_id: int):
        label, emoji, style = self.STYLES[action]
        super().__init__(discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"departed:{action}:{discord_id}"))
        self.action = action
        self.discord_id = discord_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: DuckyBot = interaction.client  # type: ignore[assignment]
        cog: Accounts = bot.get_cog("Accounts")  # type: ignore[assignment]
        if not bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen dit.", ephemeral=True)
            return
        if self.action == "ignore":
            await interaction.response.defer()
            await cog.close_report(interaction.message, f"Genegeerd door {interaction.user.mention}")
        elif self.action == "delete":
            await interaction.response.send_message(
                "Het Jellyfin-account wordt definitief verwijderd en de koppeling vervalt. Zeker weten?",
                view=ConfirmDeleteView(cog, self.discord_id, interaction.message), ephemeral=True,
            )
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
            result = await cog.disable_account(self.discord_id)
            await interaction.followup.send(result, ephemeral=True)
            if result.startswith("✅"):
                await cog.close_report(interaction.message, f"Uitgeschakeld door {interaction.user.mention}")


def departure_view(discord_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("disable", "delete", "ignore"):
        view.add_item(DepartureButton(action, discord_id))
    return view


# --- de cog ------------------------------------------------------------------

class Accounts(commands.Cog):
    def __init__(self, bot: DuckyBot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(DepartureButton)
        self.check_departures.start()
        if self.bot.jellyfin and self.bot.config.inactive_days > 0:
            self.monthly_inactive.change_interval(time=report_time(self.bot.config))
            self.monthly_inactive.start()

    async def cog_unload(self) -> None:
        self.check_departures.cancel()
        self.monthly_inactive.cancel()

    async def log_admin(self, text: str) -> None:
        await self.bot.send_to(
            self.bot.config.admin_channel_id,
            embed=discord.Embed(description=text, color=COLOR_INFO),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- vertrokken leden -------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Elk vertrek direct melden; dankzij de Server Members Intent."""
        cfg = self.bot.config
        if member.guild.id != cfg.guild_id:
            return
        link = self.bot.links.get(member.id)
        embed = leave_embed(member, discord.utils.utcnow(), await self.removal(member), link)
        view = departure_view(member.id) if link and self.bot.jellyfin else None
        await self.bot.send_to(
            cfg.leave_channel_id or cfg.admin_channel_id, embed=embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if link:  # niet nog eens via check_departures
            departed = set(self.bot.state.get("departed", []))
            if member.id not in departed:
                self.bot.state.set("departed", sorted(departed | {member.id}))

    async def removal(self, member: discord.Member) -> tuple[str, str] | None:
        """(kick/ban, door wie en waarom) uit het auditlog, als de bot dat mag lezen."""
        me = member.guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(2)  # het auditlog loopt soms iets achter op het event
        since = discord.utils.utcnow() - timedelta(seconds=30)
        try:
            async for entry in member.guild.audit_logs(limit=10):
                if entry.created_at < since:
                    break
                action = {discord.AuditLogAction.kick: "kick", discord.AuditLogAction.ban: "ban"}.get(entry.action)
                if action and entry.target and entry.target.id == member.id:
                    who = entry.user.mention if entry.user else "onbekend"
                    return action, f"{who}: {entry.reason}" if entry.reason else who
        except discord.HTTPException as exc:
            log.info("Auditlog lezen mislukt: %s", exc)
        return None

    @tasks.loop(hours=1)
    async def check_departures(self) -> None:
        """Gekoppelde leden die niet meer in de server zitten melden, één keer per vertrek.

        Vangnet voor vertrekken terwijl de bot offline was; on_member_remove meldt ze anders direct.
        """
        try:
            guild = self.bot.get_guild(self.bot.config.guild_id) or await self.bot.fetch_guild(self.bot.config.guild_id)
        except discord.HTTPException as exc:
            log.warning("Server ophalen mislukt: %s", exc)
            return
        departed = set(self.bot.state.get("departed", []))
        before = set(departed)
        for discord_id, link in self.bot.links.all().items():
            try:
                await guild.fetch_member(discord_id)
                departed.discard(discord_id)  # (weer) lid
                continue
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                log.warning("Lid %s controleren mislukt: %s", discord_id, exc)
                continue
            if discord_id not in departed:
                await self.report_departure(discord_id, link)
                departed.add(discord_id)
        if departed != before:
            self.bot.state.set("departed", sorted(departed))

    @check_departures.before_loop
    async def before_departures(self) -> None:
        await self.bot.wait_until_ready()

    async def report_departure(self, discord_id: int, link: dict[str, Any]) -> None:
        try:
            user = await self.bot.fetch_user(discord_id)
            name = f"{user.mention} ({user.name})"
        except discord.HTTPException:
            name = f"<@{discord_id}>"
        embed = discord.Embed(
            title="👋 Gekoppeld lid heeft de server verlaten",
            description=f"{name} zit niet meer in de server.",
            color=COLOR_WARN,
        )
        embed.add_field(name="Jellyseerr-account", value=link["name"])
        view = departure_view(discord_id) if self.bot.jellyfin else None
        await self.bot.send_to(
            self.bot.config.admin_channel_id, embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )

    async def close_report(self, message: discord.Message | None, outcome: str) -> None:
        if message is None or not message.embeds:
            return
        embed = message.embeds[0]
        embed.color = COLOR_ERROR if "Verwijderd" in outcome else COLOR_INFO
        embed.add_field(name="Afgehandeld", value=outcome, inline=False)
        await message.edit(embed=embed, view=None)

    async def jellyfin_account(self, discord_id: int) -> tuple[str | None, str]:
        """(Jellyfin-ID, foutmelding) bij een gekoppeld lid."""
        link = self.bot.links.get(discord_id)
        if link is None:
            return None, "❌ Dit lid is niet (meer) gekoppeld."
        try:
            jf_id = await self.bot.jellyfin_user_id(link["seerr_id"])
        except JellyseerrError as exc:
            return None, f"❌ Jellyseerr-account niet gevonden: {exc.message}"
        if jf_id is None:
            return None, "❌ Bij dit Jellyseerr-account hoort geen Jellyfin-account."
        return jf_id, ""

    async def disable_account(self, discord_id: int) -> str:
        assert self.bot.jellyfin is not None
        jf_id, error = await self.jellyfin_account(discord_id)
        if jf_id is None:
            return error
        try:
            await self.bot.jellyfin.set_disabled(jf_id)
        except JellyfinError as exc:
            return f"❌ Uitschakelen mislukt: {exc}"
        return "✅ Het Jellyfin-account is uitgeschakeld. Inschakelen kan in Jellyfin onder Gebruikers."

    async def delete_account(self, discord_id: int) -> str:
        assert self.bot.jellyfin is not None
        jf_id, error = await self.jellyfin_account(discord_id)
        if jf_id is None:
            return error
        try:
            await self.bot.jellyfin.delete_user(jf_id)
        except JellyfinError as exc:
            return f"❌ Verwijderen mislukt: {exc}"
        self.bot.links.remove(discord_id)
        return "✅ Het Jellyfin-account is verwijderd en de koppeling is opgeheven."

    # --- inactieve accounts -----------------------------------------------

    async def inactive_embed(self) -> discord.Embed:
        assert self.bot.jellyfin is not None
        cfg = self.bot.config
        now = datetime.now(cfg.tz)
        inactive = find_inactive(await self.bot.jellyfin.users(), now, cfg.inactive_days)

        # Jellyfin-account -> Discord-lid, via de Jellyseerr-accounts die gekoppeld zijn
        discord_by_seerr = {link["seerr_id"]: discord_id for discord_id, link in self.bot.links.all().items()}
        discord_by_jellyfin: dict[str, int] = {}
        try:
            for user in await self.bot.seerr.all_users():
                if discord_id := discord_by_seerr.get(user["id"]):
                    for key in (user.get("jellyfinUserId"), (user.get("jellyfinUsername") or "").casefold()):
                        if key:
                            discord_by_jellyfin[key] = discord_id
        except JellyseerrError as exc:
            log.warning("Jellyseerr-gebruikers ophalen mislukt: %s", exc)

        lines = []
        for user, last in inactive:
            discord_id = discord_by_jellyfin.get(user["Id"]) or discord_by_jellyfin.get(user.get("Name", "").casefold())
            who = f"**{user.get('Name', '?')}**" + (f" (<@{discord_id}>)" if discord_id else "")
            when = f"laatst actief {last.astimezone(cfg.tz):%d-%m-%Y} ({(now - last).days} dagen)" if last else "nooit actief"
            lines.append(f"• {who} — {when}")
        description = "\n".join(lines) if lines else "Geen inactieve accounts. 🎉"
        if len(description) > 4000:
            description = description[:3990] + "\n…"
        embed = discord.Embed(title=f"😴 Inactieve Jellyfin-accounts ({len(inactive)})", description=description, color=COLOR_INFO)
        embed.set_footer(text=f"Niets gekeken in de afgelopen {cfg.inactive_days} dagen. Beheerders en uitgeschakelde accounts tellen niet mee.")
        return embed

    @tasks.loop(hours=24)  # het echte tijdstip wordt in cog_load gezet (REPORT_TIME)
    async def monthly_inactive(self) -> None:
        now = datetime.now(self.bot.config.tz)
        month = f"{now:%Y-%m}"
        if now.day != 1 or self.bot.state.get("inactive_last") == month:
            return
        try:
            embed = await self.inactive_embed()
        except JellyfinError as exc:
            log.warning("Overzicht van inactieve accounts mislukt: %s", exc)
            return
        if await self.bot.send_to(self.bot.config.admin_channel_id, embed=embed,
                                  allowed_mentions=discord.AllowedMentions.none()) is not None:
            self.bot.state.set("inactive_last", month)

    @monthly_inactive.before_loop
    async def before_inactive(self) -> None:
        await self.bot.wait_until_ready()

    # --- commando's -------------------------------------------------------

    @app_commands.command(name="reset-password", description="Get a new temporary Jellyfin password by DM")
    @app_commands.rename(lid="member")
    @app_commands.describe(lid="(Admin) Reset the password of another member")
    async def reset_password(self, interaction: discord.Interaction, lid: discord.Member | None = None) -> None:
        jellyfin = self.bot.jellyfin
        if jellyfin is None:
            await interaction.response.send_message("Jellyfin is niet geconfigureerd voor deze bot.", ephemeral=True)
            return
        target = lid or interaction.user
        for_other = target.id != interaction.user.id
        if for_other and not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen het wachtwoord van iemand anders resetten.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        seerr_user = await self.bot.resolve_seerr_user(target)
        if seerr_user is None:
            text = f"{target.mention} is niet gekoppeld aan een Jellyseerr-account." if for_other else "Je bent nog niet gekoppeld; vraag een admin om `/link`."
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            jf_id = await self.bot.jellyfin_user_id(seerr_user["id"])
            if jf_id is None:
                await interaction.followup.send("Bij dit account hoort geen Jellyfin-account.", ephemeral=True)
                return
            jf_user = await jellyfin.user(jf_id)
            if (jf_user.get("Policy") or {}).get("IsAdministrator"):
                await interaction.followup.send(
                    "Het wachtwoord van een Jellyfin-beheerder reset de bot niet; doe dat in Jellyfin zelf.", ephemeral=True
                )
                return
            password = secrets.token_urlsafe(9)
            await jellyfin.set_password(jf_id, password)
        except (JellyfinError, JellyseerrError) as exc:
            log.warning("Wachtwoord resetten mislukt: %s", exc)
            await interaction.followup.send(f"❌ Wachtwoord resetten mislukt: {exc}", ephemeral=True)
            return

        name = jf_user.get("Name", "?")
        embed = discord.Embed(
            title="🔑 Nieuw Jellyfin-wachtwoord",
            description="Log hiermee in en kies daarna in Jellyfin een eigen wachtwoord.",
            color=COLOR_BRAND,
        )
        embed.add_field(name="Gebruikersnaam", value=f"`{name}`")
        embed.add_field(name="Wachtwoord", value=f"||`{password}`||")
        if url := self.bot.config.jellyfin_public_url:
            embed.add_field(name="Inloggen", value=url, inline=False)
        if await self.bot.dm(target.id, embed=embed):
            text = "🔑 Nieuw wachtwoord per DM verstuurd" + (f" naar {target.mention}." if for_other else ".")
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            # Geen DM mogelijk: laat het zien aan wie het commando gaf (alleen voor die persoon zichtbaar).
            note = f"⚠️ {target.mention} accepteert geen DM's; geef dit zelf door." if for_other else "Je DM's staan uit, dus hier:"
            await interaction.followup.send(note, embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        who = f"{interaction.user.mention} resette het Jellyfin-wachtwoord van {target.mention}" if for_other \
            else f"{interaction.user.mention} resette het eigen Jellyfin-wachtwoord"
        await self.log_admin(f"🔑 {who} (**{name}**).")

    @app_commands.command(name="inactive", description="(Admin) Jellyfin accounts that haven't watched anything lately")
    @app_commands.default_permissions(manage_guild=True)
    async def inactive(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen dit.", ephemeral=True)
            return
        if self.bot.jellyfin is None:
            await interaction.response.send_message("Jellyfin is niet geconfigureerd voor deze bot.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            embed = await self.inactive_embed()
        except JellyfinError as exc:
            await interaction.followup.send(f"❌ Jellyfin niet bereikbaar: {exc}", ephemeral=True)
            return
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stats", description="(Admin) Statistics about the library, accounts and requests")
    @app_commands.default_permissions(manage_guild=True)
    async def stats(self, interaction: discord.Interaction) -> None:
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Alleen admins kunnen dit.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = self.bot.config
        now = datetime.now(cfg.tz)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        embed = discord.Embed(title=f"📊 {BOT_NAME} · Ducky-Craft media", color=COLOR_BRAND, timestamp=now)

        try:
            counts = await self.bot.seerr.request_counts()
            this_month = await self.bot.seerr.requests_since(month_start)
            embed.add_field(
                name="📨 Aanvragen",
                value=(f"{counts.get('total', 0)} totaal · {counts.get('pending', 0)} in afwachting · "
                       f"{counts.get('available', 0)} beschikbaar\n{len(this_month)} deze maand"),
                inline=False,
            )
            requesters = Counter(seerr_name(r.get("requestedBy") or {}) for r in this_month)
            embed.add_field(name="🏆 Meeste aanvragen deze maand", value=top(requesters))
        except JellyseerrError as exc:
            embed.add_field(name="📨 Aanvragen", value=f"Jellyseerr niet bereikbaar: {exc.message}", inline=False)

        if jellyfin := self.bot.jellyfin:
            try:
                library, users = await asyncio.gather(jellyfin.counts(), jellyfin.users())
                active_users = [u for u in users if not (u.get("Policy") or {}).get("IsDisabled")]
                sem = asyncio.Semaphore(5)

                async def played(user: dict[str, Any]) -> tuple[str, int]:
                    async with sem:
                        return user.get("Name", "?"), await jellyfin.played_count(user["Id"])

                viewers = Counter(dict(await asyncio.gather(*(played(u) for u in active_users))))
                inactive = find_inactive(users, now, cfg.inactive_days) if cfg.inactive_days else []
                embed.insert_field_at(
                    0, name="📚 Bibliotheek", inline=False,
                    value=(f"{library.get('MovieCount', 0)} films · {library.get('SeriesCount', 0)} series · "
                           f"{library.get('EpisodeCount', 0)} afleveringen"),
                )
                accounts = (f"{len(users)} Jellyfin-accounts ({len(users) - len(active_users)} uitgeschakeld) · "
                            f"{len(self.bot.links.all())} gekoppeld aan Discord")
                if cfg.inactive_days:
                    accounts += f"\n{len(inactive)} inactief ({cfg.inactive_days}+ dagen niets gekeken)"
                embed.insert_field_at(1, name="👥 Accounts", value=accounts, inline=False)
                embed.add_field(name="🍿 Meest bekeken (titels)", value=top(viewers))
            except JellyfinError as exc:
                embed.add_field(name="Jellyfin", value=f"Niet bereikbaar: {exc}", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: DuckyBot) -> None:
    await bot.add_cog(Accounts(bot))
