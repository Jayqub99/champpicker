"""Settings for Champ Picker.

On your PC, values come from config.json (git-ignored). Environment variables
override them, which is how the Vercel site and the GitHub crawl get their
settings. Nothing secret is ever written back to disk by this module.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ON_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel only allows writing under /tmp; Data Dragon files are cached there.
DATA_DIR = Path(os.environ.get("CHAMPPICKER_DATA_DIR") or ("/tmp/champpicker" if ON_VERCEL else ROOT / "data"))
CONFIG_PATH = ROOT / "config.json"

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")

# environment variable -> config field
ENV = {"RIOT_API_KEY": "api_key", "RIOT_ID": "riot_id", "MAIN_ROLE": "role",
       "SITE_PASSWORD": "site_password", "SITE_URL": "site_url", "RIOT_PLATFORM": "platform",
       "RIOT_REGION": "region"}


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
    database_url: str = ""            # hosted Postgres; empty means the local SQLite file
    site_url: str = ""                # your Vercel site, for the champ-select companion
    site_password: str = ""           # password for the hosted site

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "pickhelper.sqlite3"

    @property
    def db_target(self) -> str:
        return self.database_url or str(self.db_path)


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    raw: dict = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    cfg = Config(**{k: v for k, v in raw.items() if k in Config.__dataclass_fields__})
    for env, attr in ENV.items():
        if os.environ.get(env):
            setattr(cfg, attr, os.environ[env].strip())
    cfg.database_url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
                        or cfg.database_url).strip()
    if os.environ.get("CHAMP_POOL"):
        cfg.pool = [x.strip() for x in os.environ["CHAMP_POOL"].split(",") if x.strip()]
    cfg.role = cfg.role.upper()
    if cfg.role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {cfg.role!r}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return cfg
