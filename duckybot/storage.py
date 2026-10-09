"""Koppelingen Discord-gebruiker <-> Jellyseerr-gebruiker, opgeslagen als JSON."""
import json
import os
import tempfile
from pathlib import Path


class LinkStore:
    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "links.json"
        self._links: dict[str, dict] = {}
        if self._path.exists():
            self._links = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".links-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._links, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    def get(self, discord_id: int) -> dict | None:
        return self._links.get(str(discord_id))

    def set(self, discord_id: int, seerr_id: int, name: str) -> None:
        self._links[str(discord_id)] = {"seerr_id": seerr_id, "name": name}
        self._save()

    def remove(self, discord_id: int) -> bool:
        if self._links.pop(str(discord_id), None) is None:
            return False
        self._save()
        return True

    def discord_ids_for(self, *, seerr_id: int | None = None, name: str | None = None) -> list[int]:
        """Omgekeerd opzoeken: welke Discord-gebruikers horen bij deze Jellyseerr-gebruiker?"""
        wanted = name.casefold() if name else None
        return [
            int(discord_id)
            for discord_id, link in self._links.items()
            if (seerr_id is not None and link["seerr_id"] == seerr_id)
            or (wanted and link["name"].casefold() == wanted)
        ]
