"""Minimale Jellyfin-client: alleen wat nodig is om gebruikers uit te nodigen."""
from typing import Any

import aiohttp


class JellyfinError(Exception):
    pass


class Jellyfin:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str):
        self._session = session
        self._base = base_url
        self._headers = {
            "Authorization": f'MediaBrowser Client="ducky-craft-bot", Token="{api_key}"',
            "Accept": "application/json",
        }

    async def _call(self, method: str, path: str, **kwargs) -> Any:
        async with self._session.request(
            method, f"{self._base}{path}", headers=self._headers, timeout=aiohttp.ClientTimeout(total=20), **kwargs
        ) as resp:
            if resp.status >= 400:
                raise JellyfinError(f"Jellyfin gaf {resp.status}: {(await resp.text())[:200]}")
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
