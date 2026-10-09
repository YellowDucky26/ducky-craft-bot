"""Kleine async client voor de Jellyseerr/Seerr API (v1)."""
import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

TMDB_POSTER = "https://image.tmdb.org/t/p/w500"

# MediaInfo.status
MEDIA_STATUS = {
    1: "Onbekend",
    2: "In afwachting",
    3: "Wordt verwerkt",
    4: "Deels beschikbaar",
    5: "Beschikbaar",
    6: "Verwijderd",
}
# MediaRequest.status
REQUEST_STATUS = {1: "In afwachting", 2: "Goedgekeurd", 3: "Afgewezen", 4: "Mislukt", 5: "Voltooid"}
ACTIVE_REQUEST_STATUSES = {1, 2, 5}


class JellyseerrError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def discord_ids_of(settings: dict[str, Any]) -> list[str]:
    """Discord-ID's uit gebruikersnotificatie-instellingen.

    Seerr gebruikt ``discordIds`` (lijst), oudere Jellyseerr ``discordId`` (string).
    """
    ids = settings.get("discordIds") or []
    if isinstance(ids, str):
        ids = [ids]
    legacy = settings.get("discordId")
    if legacy:
        ids = [*ids, legacy]
    return [str(i).strip() for i in ids if str(i).strip()]


def title_of(media: dict[str, Any]) -> str:
    return media.get("title") or media.get("name") or "Onbekende titel"


def year_of(media: dict[str, Any]) -> str:
    date = media.get("releaseDate") or media.get("firstAirDate") or ""
    return date[:4]


def poster_of(media: dict[str, Any]) -> str | None:
    path = media.get("posterPath")
    return f"{TMDB_POSTER}{path}" if path else None


def requestable_seasons(tv: dict[str, Any]) -> list[int]:
    """Seizoenen (excl. specials) die nog niet beschikbaar of aangevraagd zijn."""
    info = tv.get("mediaInfo") or {}
    taken = {s["seasonNumber"] for s in info.get("seasons") or [] if s.get("status", 1) in (2, 3, 4, 5)}
    for request in info.get("requests") or []:
        if request.get("status") in ACTIVE_REQUEST_STATUSES:
            taken.update(s["seasonNumber"] for s in request.get("seasons") or [])
    return [
        s["seasonNumber"]
        for s in tv.get("seasons") or []
        if s.get("seasonNumber", 0) > 0 and s["seasonNumber"] not in taken
    ]


class Jellyseerr:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str):
        self._session = session
        self._base = f"{base_url}/api/v1"
        self._api_key = api_key

    async def _call(self, method: str, path: str, *, as_user: int | None = None, **kwargs) -> Any:
        headers = {"X-Api-Key": self._api_key, "Accept": "application/json"}
        if as_user is not None:
            # Handel namens deze gebruiker: diens rechten, quota en auto-approve tellen.
            headers["X-Api-User"] = str(as_user)
        async with self._session.request(
            method, f"{self._base}{path}", headers=headers, timeout=aiohttp.ClientTimeout(total=20), **kwargs
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                data = None
            if resp.status >= 400:
                message = (data or {}).get("message") or (data or {}).get("error") or resp.reason
                raise JellyseerrError(resp.status, str(message))
            return data

    # --- media -----------------------------------------------------------

    async def search(self, query: str) -> list[dict[str, Any]]:
        data = await self._call("GET", "/search", params={"query": query, "page": 1})
        return [r for r in data.get("results", []) if r.get("mediaType") in ("movie", "tv")]

    async def media(self, media_type: str, tmdb_id: int) -> dict[str, Any]:
        return await self._call("GET", f"/{media_type}/{tmdb_id}")

    async def create_request(
        self, user_id: int, media_type: str, tmdb_id: int, seasons: list[int] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == "tv":
            body["seasons"] = seasons or []
        return await self._call("POST", "/request", json=body, as_user=user_id)

    async def get_request(self, request_id: int) -> dict[str, Any]:
        return await self._call("GET", f"/request/{request_id}")

    async def set_request_status(self, request_id: int, status: str, as_user: int | None = None) -> dict[str, Any]:
        """status: 'approve' of 'decline'."""
        return await self._call("POST", f"/request/{request_id}/{status}", as_user=as_user)

    async def user_requests(self, user_id: int, take: int = 10) -> list[dict[str, Any]]:
        data = await self._call("GET", f"/user/{user_id}/requests", params={"take": take, "skip": 0})
        return data.get("results", [])

    # --- gebruikers ------------------------------------------------------

    async def user(self, user_id: int) -> dict[str, Any]:
        return await self._call("GET", f"/user/{user_id}")

    async def users(self, query: str | None = None, take: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"take": take, "skip": 0, "sort": "displayname"}
        if query:
            params["q"] = query
        data = await self._call("GET", "/user", params=params)
        return data.get("results", [])

    async def all_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        skip = 0
        while True:
            data = await self._call("GET", "/user", params={"take": 100, "skip": skip})
            batch = data.get("results", [])
            users.extend(batch)
            skip += len(batch)
            if not batch or skip >= data.get("pageInfo", {}).get("results", 0):
                return users

    async def notification_settings(self, user_id: int) -> dict[str, Any]:
        return await self._call("GET", f"/user/{user_id}/settings/notifications")

    async def set_discord_id(self, user_id: int, discord_id: str) -> None:
        """Zet het Discord-ID in het Jellyseerr-profiel (zowel nieuwe als oude veldnaam)."""
        settings = await self.notification_settings(user_id)
        settings["discordIds"] = [discord_id]
        settings["discordId"] = discord_id
        await self._call("POST", f"/user/{user_id}/settings/notifications", json=settings)

    async def find_user_by_discord_id(self, discord_id: str) -> dict[str, Any] | None:
        """Zoek de Jellyseerr-gebruiker die dit Discord-ID in zijn profiel heeft staan."""
        users = await self.all_users()
        sem = asyncio.Semaphore(5)

        async def check(user: dict[str, Any]) -> dict[str, Any] | None:
            async with sem:
                try:
                    settings = await self.notification_settings(user["id"])
                except JellyseerrError as exc:
                    log.debug("Notificatie-instellingen van %s niet leesbaar: %s", user["id"], exc)
                    return None
            return user if discord_id in discord_ids_of(settings) else None

        for match in await asyncio.gather(*(check(u) for u in users)):
            if match:
                return match
        return None

    async def import_jellyfin_user(self, jellyfin_user_id: str) -> dict[str, Any] | None:
        created = await self._call("POST", "/user/import-from-jellyfin", json={"jellyfinUserIds": [jellyfin_user_id]})
        if created:
            return created[0]
        # Bestond al: opzoeken via het Jellyfin-ID.
        for user in await self.all_users():
            if user.get("jellyfinUserId") == jellyfin_user_id:
                return user
        return None
