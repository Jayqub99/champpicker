"""SQLite storage for crawled matches and your own games."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id   TEXT PRIMARY KEY,
    patch      TEXT NOT NULL,
    queue_id   INTEGER,
    game_start INTEGER,
    duration   INTEGER
);
CREATE TABLE IF NOT EXISTS participants (
    match_id    TEXT NOT NULL,
    team_id     INTEGER NOT NULL,
    champion_id INTEGER NOT NULL,
    position    TEXT NOT NULL,
    win         INTEGER NOT NULL,
    phys_dmg    INTEGER, magic_dmg INTEGER, true_dmg INTEGER,
    dmg_taken   INTEGER, cc_time INTEGER,
    PRIMARY KEY (match_id, team_id, position)
);
CREATE INDEX IF NOT EXISTS ix_part_champ ON participants (champion_id, position);
CREATE INDEX IF NOT EXISTS ix_part_match ON participants (match_id);
CREATE TABLE IF NOT EXISTS players (
    puuid        TEXT PRIMARY KEY,
    tier         TEXT,
    last_crawled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS my_games (
    match_id    TEXT PRIMARY KEY,
    champion_id INTEGER NOT NULL,
    position    TEXT,
    win         INTEGER NOT NULL,
    game_start  INTEGER
);
CREATE TABLE IF NOT EXISTS my_mastery (
    champion_id INTEGER PRIMARY KEY,
    points      INTEGER,
    last_played INTEGER
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def patch_of(game_version: str) -> str:
    parts = game_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


def store_match(conn: sqlite3.Connection, match: dict) -> bool:
    """Save a match-v5 payload. Returns False for remakes or games without roles."""
    info = match.get("info", {})
    if info.get("gameDuration", 0) < 300:
        return False
    parts = info.get("participants", [])
    if len(parts) != 10 or any(not p.get("teamPosition") for p in parts):
        return False
    mid = match["metadata"]["matchId"]
    conn.execute("INSERT OR IGNORE INTO matches VALUES (?,?,?,?,?)",
                 (mid, patch_of(info.get("gameVersion", "")), info.get("queueId"),
                  info.get("gameStartTimestamp"), info.get("gameDuration")))
    for p in parts:
        conn.execute(
            "INSERT OR IGNORE INTO participants VALUES (?,?,?,?,?,?,?,?,?,?)",
            (mid, p["teamId"], p["championId"], p["teamPosition"], int(bool(p["win"])),
             p.get("physicalDamageDealtToChampions", 0), p.get("magicDamageDealtToChampions", 0),
             p.get("trueDamageDealtToChampions", 0), p.get("totalDamageTaken", 0),
             p.get("timeCCingOthers", 0)))
    return True


def has_match(conn: sqlite3.Connection, match_id: str) -> bool:
    return conn.execute("SELECT 1 FROM matches WHERE match_id=?", (match_id,)).fetchone() is not None
