"""Opslag als JSON in de datamap: koppelingen (links.json) en bijgehouden status (state.json)."""
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: Path, data: Any) -> None:
    """Atomisch schrijven: eerst een tijdelijk bestand, dan hernoemen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


class LinkStore:
    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "links.json"
        self._links: dict[str, dict] = {}
        if self._path.exists():
            self._links = json.loads(self._path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        write_json(self._path, self._links)

    def check_writable(self) -> None:
        """Stop direct bij het starten als de datamap niet schrijfbaar is, niet pas bij /link."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=self._path.parent, prefix=".schrijftest-"):
                pass
        except OSError as exc:
            uid = os.getuid() if hasattr(os, "getuid") else "?"
            raise SystemExit(
                f"Kan niet schrijven in de datamap {self._path.parent} ({exc.strerror}). "
                f"De bot draait als uid {uid}; geef die eigenaar van de map, "
                f"bijvoorbeeld: sudo chown {uid}:{uid} <map op de host>"
            ) from exc

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

    def all(self) -> dict[int, dict]:
        return {int(discord_id): link for discord_id, link in self._links.items()}

    def discord_ids_for(self, *, seerr_id: int | None = None, name: str | None = None) -> list[int]:
        """Omgekeerd opzoeken: welke Discord-gebruikers horen bij deze Jellyseerr-gebruiker?"""
        wanted = name.casefold() if name else None
        return [
            int(discord_id)
            for discord_id, link in self._links.items()
            if (seerr_id is not None and link["seerr_id"] == seerr_id)
            or (wanted and link["name"].casefold() == wanted)
        ]


class StateStore:
    """Wat de bot moet onthouden tussen herstarts, zoals wanneer het laatste overzicht is verstuurd."""

    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "state.json"
        self._state: dict[str, Any] = {}
        if self._path.exists():
            self._state = json.loads(self._path.read_text(encoding="utf-8"))

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value
        write_json(self._path, self._state)
