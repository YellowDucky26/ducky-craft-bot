"""Configuratie uit environment-variabelen."""
import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 0 = maandag, zoals datetime.weekday()
WEEKDAYS = {
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3, "vrijdag": 4, "zaterdag": 5, "zondag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}
DIGEST_MODES = ("daily", "weekly", "off")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    value = _str(name)
    return int(value) if value else default


def _ids(name: str) -> frozenset[int]:
    return frozenset(int(part) for part in _str(name).replace(" ", "").split(",") if part)


def _url(name: str, default: str = "") -> str:
    return _str(name, default).rstrip("/")


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int

    jellyseerr_url: str
    jellyseerr_api_key: str
    jellyseerr_public_url: str = ""

    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    jellyfin_public_url: str = ""

    # Rollen: admins mogen alles (goedkeuren, meldingen, koppelen, invites);
    # invite-rollen mogen alleen /invite discord gebruiken.
    admin_role_ids: frozenset[int] = field(default_factory=frozenset)
    invite_role_ids: frozenset[int] = field(default_factory=frozenset)
    # Optioneel: rol die iemand krijgt zodra er een Jellyfin-account voor is gemaakt.
    member_role_id: int = 0

    request_channel_id: int = 0   # openbare meldingen (goedgekeurd, beschikbaar)
    admin_channel_id: int = 0     # nieuwe aanvragen met knoppen, fouten, issues
    announce_channel_id: int = 0  # standaardkanaal voor /announce en /notify
    invite_channel_id: int = 0    # kanaal waarvoor Discord-invites worden gemaakt

    # Geplande berichten: overzicht van nieuwe titels en het maandoverzicht van inactieve accounts.
    timezone: str = "Europe/Amsterdam"
    report_time: str = "18:00"     # HH:MM in de tijdzone hierboven
    digest: str = "weekly"         # daily, weekly of off
    digest_day: int = 4            # bij weekly: 0 = maandag ... 4 = vrijdag
    digest_channel_id: int = 0     # standaard het aankondigingskanaal
    inactive_days: int = 60        # 0 = geen maandoverzicht

    web_port: int = 8688
    webhook_secret: str = ""
    data_dir: str = "/data"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def jellyfin_enabled(self) -> bool:
        return bool(self.jellyfin_url and self.jellyfin_api_key)

    @classmethod
    def from_env(cls) -> "Config":
        jellyseerr_url = _url("JELLYSEERR_URL")
        jellyfin_url = _url("JELLYFIN_URL")
        cfg = cls(
            discord_token=_str("DISCORD_TOKEN"),
            guild_id=_int("GUILD_ID"),
            jellyseerr_url=jellyseerr_url,
            jellyseerr_api_key=_str("JELLYSEERR_API_KEY"),
            jellyseerr_public_url=_url("JELLYSEERR_PUBLIC_URL", jellyseerr_url),
            jellyfin_url=jellyfin_url,
            jellyfin_api_key=_str("JELLYFIN_API_KEY"),
            jellyfin_public_url=_url("JELLYFIN_PUBLIC_URL", jellyfin_url),
            admin_role_ids=_ids("ADMIN_ROLE_IDS"),
            invite_role_ids=_ids("INVITE_ROLE_IDS"),
            member_role_id=_int("MEMBER_ROLE_ID"),
            request_channel_id=_int("REQUEST_CHANNEL_ID"),
            admin_channel_id=_int("ADMIN_CHANNEL_ID"),
            announce_channel_id=_int("ANNOUNCE_CHANNEL_ID"),
            invite_channel_id=_int("INVITE_CHANNEL_ID"),
            timezone=_str("TZ", "Europe/Amsterdam"),
            report_time=_str("REPORT_TIME", "18:00"),
            digest=_str("DIGEST", "weekly").lower(),
            digest_day=WEEKDAYS.get(_str("DIGEST_DAY", "vrijdag").lower(), -1),
            digest_channel_id=_int("DIGEST_CHANNEL_ID"),
            inactive_days=_int("INACTIVE_DAYS", 60),
            web_port=_int("WEB_PORT", 8688),
            webhook_secret=_str("WEBHOOK_SECRET"),
            data_dir=_str("DATA_DIR", "/data"),
        )
        missing = [
            name
            for name, value in (
                ("DISCORD_TOKEN", cfg.discord_token),
                ("GUILD_ID", cfg.guild_id),
                ("JELLYSEERR_URL", cfg.jellyseerr_url),
                ("JELLYSEERR_API_KEY", cfg.jellyseerr_api_key),
                ("WEBHOOK_SECRET", cfg.webhook_secret),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"Ontbrekende configuratie: {', '.join(missing)}")
        problems = []
        if cfg.digest not in DIGEST_MODES:
            problems.append(f"DIGEST moet daily, weekly of off zijn, niet '{cfg.digest}'")
        if cfg.digest_day < 0:
            problems.append(f"DIGEST_DAY is geen weekdag: '{_str('DIGEST_DAY')}'")
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", cfg.report_time):
            problems.append(f"REPORT_TIME moet HH:MM zijn, niet '{cfg.report_time}'")
        try:
            ZoneInfo(cfg.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(f"TZ is geen bekende tijdzone: '{cfg.timezone}'")
        if problems:
            raise SystemExit("Ongeldige configuratie: " + "; ".join(problems))
        return cfg
