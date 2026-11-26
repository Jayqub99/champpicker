"""Champion, item, rune and summoner spell data from Riot's Data Dragon (no key needed)."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

BASE = "https://ddragon.leagueoflegends.com"
USER_AGENT = "champpicker/0.1 (personal League draft helper)"

# Stat shards aren't in Data Dragon's rune file.
SHARDS = {5008: "Adaptive Force", 5005: "Attack Speed", 5007: "Ability Haste",
          5001: "Health Scaling", 5011: "Health", 5013: "Tenacity and Slow Resist",
          5010: "Move Speed", 5002: "Armor", 5003: "Magic Resist"}

FILES = {"champion": "data/en_US/champion.json", "item": "data/en_US/item.json",
         "summoner": "data/en_US/summoner.json", "runes": "data/en_US/runesReforged.json"}


def _get(url: str, timeout: int):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


class Champions:
    """All static game data for one patch (named for its first job: champions)."""

    def __init__(self, version: str, data: dict, items: dict | None = None,
                 summoners: dict | None = None, runes: list | None = None):
        self.version = version
        cdn = f"{BASE}/cdn/{version}/img"
        self.by_id: dict[int, dict] = {}
        self.by_name: dict[str, int] = {}
        for c in data["data"].values():
            cid = int(c["key"])
            self.by_id[cid] = {"id": cid, "name": c["name"], "slug": c["id"],
                               "tags": c.get("tags", []), "title": c.get("title", ""),
                               "image": f"{cdn}/champion/{c['image']['full']}"}
            self.by_name[_norm(c["name"])] = cid
            self.by_name[_norm(c["id"])] = cid

        self.items: dict[int, dict] = {}
        for key, it in ((items or {}).get("data") or {}).items():
            iid = int(key)
            tags = it.get("tags", [])
            gold = (it.get("gold") or {}).get("total", 0)
            on_rift = (it.get("maps") or {}).get("11", True)
            boots = "Boots" in tags and gold > 300
            final = (not it.get("into") and gold >= 2000
                     and "Consumable" not in tags and "Trinket" not in tags)
            self.items[iid] = {"id": iid, "name": it.get("name", str(iid)), "gold": gold,
                               "boots": bool(on_rift and boots),
                               "legendary": bool(on_rift and final and not boots),
                               "image": f"{cdn}/item/{(it.get('image') or {}).get('full', f'{iid}.png')}"}

        self.spells: dict[int, dict] = {}
        for s in ((summoners or {}).get("data") or {}).values():
            sid = int(s["key"])
            self.spells[sid] = {"id": sid, "name": s["name"],
                                "image": f"{cdn}/spell/{s['image']['full']}"}

        self.runes: dict[int, dict] = {}
        self.rune_trees: dict[int, list[list[int]]] = {}  # style -> rows of rune ids
        self._abilities: dict[str, dict | None] = {}
        self._cdn = cdn
        for style in runes or []:
            self.rune_trees[style["id"]] = [[r["id"] for r in slot.get("runes", [])]
                                            for slot in style.get("slots", [])]
            self.runes[style["id"]] = {"id": style["id"], "name": style["name"],
                                       "image": f"{BASE}/cdn/img/{style['icon']}"}
            for slot in style.get("slots", []):
                for r in slot.get("runes", []):
                    self.runes[r["id"]] = {"id": r["id"], "name": r["name"],
                                           "image": f"{BASE}/cdn/img/{r['icon']}"}
        for sid, name in SHARDS.items():
            self.runes.setdefault(sid, {"id": sid, "name": name, "image": None})

    def tree(self, style_id: int) -> list[list[dict]]:
        """Every rune in a rune tree, row by row (the first row holds the keystones)."""
        return [[self.rune(r) for r in row] for row in self.rune_trees.get(style_id, [])]

    def abilities(self, cid: int, fetch=None) -> dict | None:
        """Passive and Q/W/E/R names and icons, fetched once per champion."""
        slug = self.by_id.get(cid, {}).get("slug")
        if not slug:
            return None
        if slug not in self._abilities:
            try:
                with (fetch or _get)(f"{BASE}/cdn/{self.version}/data/en_US/champion/{slug}.json", timeout=10) as r:
                    data = json.loads(r.read())["data"][slug]
                spells = data.get("spells", [])
                self._abilities[slug] = {
                    "passive": {"key": "P", "name": data["passive"]["name"],
                                "image": f"{self._cdn}/passive/{data['passive']['image']['full']}"},
                    **{k: {"key": k, "name": sp["name"], "image": f"{self._cdn}/spell/{sp['image']['full']}"}
                       for k, sp in zip("QWER", spells)}}
            except (OSError, KeyError, ValueError):
                return None  # not cached, so the next page view tries again
        return self._abilities[slug]

    def name(self, cid: int) -> str:
        return self.by_id.get(cid, {}).get("name", f"#{cid}")

    def item(self, iid: int) -> dict:
        return self.items.get(iid, {"id": iid, "name": f"Item {iid}", "image": None,
                                    "legendary": False, "boots": False, "gold": 0})

    def is_completed(self, iid: int) -> bool:
        it = self.items.get(iid)
        return bool(it and (it["legendary"] or it["boots"]))

    def spell(self, sid: int) -> dict:
        return self.spells.get(sid, {"id": sid, "name": f"Spell {sid}", "image": None})

    def rune(self, rid: int) -> dict:
        return self.runes.get(rid, {"id": rid, "name": f"Rune {rid}", "image": None})

    def find(self, text: str) -> int | None:
        text = str(text).strip()
        if text.isdigit():
            return int(text) if int(text) in self.by_id else None
        return self.by_name.get(_norm(text))

    def list(self) -> list[dict]:
        return sorted(self.by_id.values(), key=lambda c: c["name"])


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def load(data_dir: Path, fetch=_get) -> Champions:
    """Latest game data, cached on disk so it still works offline."""
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        with fetch(f"{BASE}/api/versions.json", timeout=15) as r:
            version = json.loads(r.read())[0]
        for kind, path in FILES.items():
            cache = data_dir / f"{kind}_{version}.json"
            if not cache.exists():
                with fetch(f"{BASE}/cdn/{version}/{path}", timeout=30) as r:
                    cache.write_bytes(r.read())
    except OSError:
        cached = sorted(data_dir.glob("champion_*.json"))
        if not cached:
            raise RuntimeError("Can't reach Data Dragon and no cached champion list exists yet.")
        version = cached[-1].stem.split("_", 1)[1]

    def read(kind):
        f = data_dir / f"{kind}_{version}.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

    return Champions(version, read("champion"), read("item"), read("summoner"), read("runes"))
