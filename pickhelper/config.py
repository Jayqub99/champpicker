"""Settings for the pick helper.

Values come from pickhelper/config.json (git-ignored) with environment
variables taking priority for the Riot API key. Nothing secret is ever
written back to disk by this module.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


@dataclass
class Config:
    riot_id: str = ""                 # e.g. "Name#NA1"
    platform: str = "na1"             # platform routing (league-v4, mastery)
    region: str = "americas"          # regional routing (account-v1, match-v5)
    role: str = "BOTTOM"              # your main role
    pool: list[str] = field(default_factory=list)  # optional fixed champion pool (names)
    crawl_tiers: list[str] = field(default_factory=lambda: ["EMERALD", "DIAMOND", "MASTER"])
    patches_to_use: int = 2           # how many recent patches feed the stats
    api_key: str = ""

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "pickhelper.sqlite3"


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    raw: dict = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})
    env_key = os.environ.get("RIOT_API_KEY")
    if env_key:
        cfg.api_key = env_key.strip()
    cfg.role = cfg.role.upper()
    if cfg.role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {cfg.role!r}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return cfg
