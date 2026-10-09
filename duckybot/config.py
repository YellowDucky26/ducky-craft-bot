"""Configuratie uit environment-variabelen."""
import os
from dataclasses import dataclass, field


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
    # invite-rollen mogen alleen /invite gebruiken.
    admin_role_ids: frozenset[int] = field(default_factory=frozenset)
    invite_role_ids: frozenset[int] = field(default_factory=frozenset)
    # Optioneel: rol die iemand krijgt zodra er een Jellyfin-account voor is gemaakt.
    member_role_id: int = 0

    request_channel_id: int = 0   # openbare meldingen (goedgekeurd, beschikbaar)
    admin_channel_id: int = 0     # nieuwe aanvragen met knoppen, fouten, issues
    announce_channel_id: int = 0  # standaardkanaal voor /melding en /notify
    invite_channel_id: int = 0    # kanaal waarvoor Discord-invites worden gemaakt

    web_port: int = 8688
    webhook_secret: str = ""
    data_dir: str = "/data"

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
        return cfg
