"""Kleine Jellyfin-client: gebruikers, sessies en de bibliotheek."""
import re
from datetime import datetime, timezone
from typing import Any

import aiohttp


class JellyfinError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def parse_date(value: str | None) -> datetime | None:
    """Jellyfin-datums als '2026-10-11T19:45:31.1234567Z' (7 decimalen) naar een aware datetime in UTC."""
    if not value:
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))  # Python kan max. 6 decimalen
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def image_url(public_base: str, item_id: str) -> str:
    return f"{public_base}/Items/{item_id}/Images/Primary?maxHeight=300"


def details_url(public_base: str, item_id: str) -> str:
    return f"{public_base}/web/#/details?id={item_id}"


class Jellyfin:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str):
        self._session = session
        self._base = base_url
        self._headers = {
            "Authorization": f'MediaBrowser Client="Kwakkel", Token="{api_key}"',
            "Accept": "application/json",
        }

    async def _call(self, method: str, path: str, **kwargs) -> Any:
        async with self._session.request(
            method, f"{self._base}{path}", headers=self._headers, timeout=aiohttp.ClientTimeout(total=20), **kwargs
        ) as resp:
            if resp.status >= 400:
                raise JellyfinError(f"Jellyfin gaf {resp.status}: {(await resp.text())[:200]}", resp.status)
            if resp.content_type == "application/json":
                return await resp.json()
            return None

    async def user_exists(self, name: str) -> bool:
        users = await self._call("GET", "/Users")
        return any(u.get("Name", "").casefold() == name.casefold() for u in users or [])

    async def create_user(self, name: str, password: str) -> dict[str, Any]:
        return await self._call("POST", "/Users/New", json={"Name": name, "Password": password})

    async def delete_user(self, user_id: str) -> None:
        await self._call("DELETE", f"/Users/{user_id}")

    # --- gebruikers ------------------------------------------------------

    async def users(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/Users") or []

    async def user(self, user_id: str) -> dict[str, Any]:
        return await self._call("GET", f"/Users/{user_id}")

    async def set_password(self, user_id: str, password: str) -> None:
        body = {"NewPw": password, "ResetPassword": False}
        try:
            await self._call("POST", "/Users/Password", params={"userId": user_id}, json=body)  # Jellyfin 10.9+
        except JellyfinError as exc:
            if exc.status not in (404, 405):
                raise
            await self._call("POST", f"/Users/{user_id}/Password", json=body)  # oudere versies

    async def set_disabled(self, user_id: str, disabled: bool = True) -> None:
        policy = (await self.user(user_id))["Policy"]
        policy["IsDisabled"] = disabled
        await self._call("POST", f"/Users/{user_id}/Policy", json=policy)

    # --- sessies en bibliotheek ------------------------------------------

    async def now_playing(self) -> list[dict[str, Any]]:
        sessions = await self._call("GET", "/Sessions", params={"activeWithinSeconds": 960}) or []
        return [s for s in sessions if s.get("NowPlayingItem")]

    async def items_added_since(self, since: datetime, limit: int = 300) -> list[dict[str, Any]]:
        """Films en afleveringen die na ``since`` aan de bibliotheek zijn toegevoegd, nieuwste eerst."""
        data = await self._call("GET", "/Items", params={
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Fields": "DateCreated,Overview",
            "Limit": limit,
        })
        items = []
        for item in (data or {}).get("Items", []):
            created = parse_date(item.get("DateCreated"))
            if created is None or created <= since:
                break
            items.append(item)
        return items

    async def counts(self) -> dict[str, int]:
        return await self._call("GET", "/Items/Counts") or {}

    async def played_count(self, user_id: str) -> int:
        data = await self._call("GET", "/Items", params={
            "userId": user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Filters": "IsPlayed",
            "Limit": 0,
        })
        return int((data or {}).get("TotalRecordCount", 0))
