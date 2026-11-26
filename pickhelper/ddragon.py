"""Champion names and images from Riot's Data Dragon (no API key needed)."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

BASE = "https://ddragon.leagueoflegends.com"


class Champions:
    def __init__(self, version: str, data: dict):
        self.version = version
        self.by_id: dict[int, dict] = {}
        self.by_name: dict[str, int] = {}
        for c in data["data"].values():
            cid = int(c["key"])
            self.by_id[cid] = {"id": cid, "name": c["name"], "slug": c["id"],
                               "tags": c.get("tags", []),
                               "image": f"{BASE}/cdn/{version}/img/champion/{c['image']['full']}"}
            self.by_name[_norm(c["name"])] = cid
            self.by_name[_norm(c["id"])] = cid

    def name(self, cid: int) -> str:
        return self.by_id.get(cid, {}).get("name", f"#{cid}")

    def find(self, text: str) -> int | None:
        text = str(text).strip()
        if text.isdigit():
            return int(text) if int(text) in self.by_id else None
        return self.by_name.get(_norm(text))

    def list(self) -> list[dict]:
        return sorted(self.by_id.values(), key=lambda c: c["name"])


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def load(data_dir: Path, fetch=urllib.request.urlopen) -> Champions:
    """Latest champion list, cached on disk so it still works offline."""
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with fetch(f"{BASE}/api/versions.json", timeout=15) as r:
            version = json.loads(r.read())[0]
        cache = data_dir / f"champion_{version}.json"
        if not cache.exists():
            with fetch(f"{BASE}/cdn/{version}/data/en_US/champion.json", timeout=30) as r:
                cache.write_bytes(r.read())
    except OSError:
        cached = sorted(data_dir.glob("champion_*.json"))
        if not cached:
            raise RuntimeError("Can't reach Data Dragon and no cached champion list exists yet.")
        cache = cached[-1]
        version = cache.stem.split("_", 1)[1]
    return Champions(version, json.loads(cache.read_text(encoding="utf-8")))
