"""Storage for crawled matches and your own games.

Works with a local SQLite file (the default on your PC) or a hosted Postgres
database (set DATABASE_URL), which is what the Vercel deployment and the
nightly GitHub crawl use. All SQL here is written to run on both.
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

TABLES = [
    """CREATE TABLE IF NOT EXISTS matches (
        match_id   TEXT PRIMARY KEY,
        patch      TEXT NOT NULL,
        queue_id   INTEGER,
        game_start BIGINT,
        duration   INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS participants (
        match_id    TEXT NOT NULL,
        team_id     INTEGER NOT NULL,
        champion_id INTEGER NOT NULL,
        position    TEXT NOT NULL,
        win         INTEGER NOT NULL,
        phys_dmg    INTEGER, magic_dmg INTEGER, true_dmg INTEGER,
        dmg_taken   INTEGER, cc_time INTEGER,
        PRIMARY KEY (match_id, team_id, position)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_part_champ ON participants (champion_id, position)",
    "CREATE INDEX IF NOT EXISTS ix_part_match ON participants (match_id)",
    """CREATE TABLE IF NOT EXISTS bans (
        match_id    TEXT NOT NULL,
        team_id     INTEGER NOT NULL,
        pick_turn   INTEGER NOT NULL,
        champion_id INTEGER NOT NULL,
        PRIMARY KEY (match_id, team_id, pick_turn)
    )""",
    """CREATE TABLE IF NOT EXISTS players (
        puuid        TEXT PRIMARY KEY,
        tier         TEXT,
        last_crawled BIGINT DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS my_games (
        match_id    TEXT PRIMARY KEY,
        champion_id INTEGER NOT NULL,
        position    TEXT,
        win         INTEGER NOT NULL,
        game_start  BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS my_mastery (
        champion_id INTEGER PRIMARY KEY,
        points      BIGINT,
        last_played BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS live_state (
        id      INTEGER PRIMARY KEY,
        payload TEXT,
        updated BIGINT
    )""",
]

# Columns added after the first release; older databases get them on connect.
ADDED_COLUMNS = {
    "matches": [("has_builds", "INTEGER DEFAULT 0"), ("has_timeline", "INTEGER DEFAULT 0")],
    "participants": [
        ("items", "TEXT"),        # final items, slots 0-5, comma separated
        ("spells", "TEXT"),       # summoner spell ids, sorted, e.g. "4,7"
        ("runes", "TEXT"),        # keystone first, then the other 5 runes
        ("styles", "TEXT"),       # primary style, secondary style
        ("shards", "TEXT"),       # offense, flex, defense stat shards
        ("kills", "INTEGER"), ("deaths", "INTEGER"), ("assists", "INTEGER"), ("cs", "INTEGER"),
        ("skill_order", "TEXT"),  # from the timeline, e.g. "QWEQQRQWQW..."
        ("start_items", "TEXT"),  # bought in the first 90 seconds
        ("item_order", "TEXT"),   # order completed items were bought
    ],
}

PARTICIPANT_COLS = ["match_id", "team_id", "champion_id", "position", "win", "phys_dmg", "magic_dmg",
                    "true_dmg", "dmg_taken", "cc_time", "items", "spells", "runes", "styles", "shards",
                    "kills", "deaths", "assists", "cs"]


class Row(tuple):
    """A result row readable by position (row[0]) or by column name (row["win"])."""

    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return super().__getitem__(self._names[key])
        return super().__getitem__(key)

    def keys(self):
        return list(self._names)


class Database:
    """Thin wrapper giving SQLite and Postgres the same small interface."""

    def __init__(self, raw, kind: str):
        self.raw = raw
        self.kind = kind

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "postgres" else sql

    def execute(self, sql: str, params=()):
        if self.kind == "sqlite":
            return self.raw.execute(sql, params)
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql: str, rows):
        if self.kind == "sqlite":
            return self.raw.executemany(sql, rows)
        with self.raw.cursor() as cur:
            cur.executemany(self._sql(sql), rows)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def _pg_row_factory(cursor):
    names = {d.name: i for i, d in enumerate(cursor.description or [])}
    return lambda values: Row(values, names)


def _sqlite_row_factory(cursor, values):
    return Row(values, {d[0]: i for i, d in enumerate(cursor.description)})


DEFAULT_SCHEMA = "champpicker"


def _check_pooler(url: str) -> None:
    """Transaction-mode poolers (Supabase port 6543, pgbouncer) drop session settings
    between statements, so our separate schema could silently fall back to `public`
    and mix with other apps' tables. Refuse them."""
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    if parsed.port == 6543 or parse_qs(parsed.query).get("pgbouncer") == ["true"]:
        raise RuntimeError("That's a transaction-pooler connection string (port 6543). "
                           "Use Supabase's Session pooler or direct connection string (port 5432).")


def connect(target: Path | str) -> Database:
    """Open a SQLite file (a path or ":memory:") or a Postgres URL.

    On Postgres every table lives in its own schema (DB_SCHEMA, default
    "champpicker"), so sharing a database with another app, such as a Supabase
    project, can never touch that app's tables.
    """
    target = str(target)
    if target.startswith(("postgres://", "postgresql://")):
        import psycopg  # only needed for the hosted setup
        _check_pooler(target)
        schema = os.environ.get("DB_SCHEMA", DEFAULT_SCHEMA)
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", schema):
            raise ValueError(f"Bad DB_SCHEMA {schema!r}: use lowercase letters, digits and _")
        raw = psycopg.connect(target, row_factory=_pg_row_factory, prepare_threshold=None)
        if "search_path" not in target:   # tests pick their own schema in the URL
            raw.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            raw.execute(f"SET search_path TO {schema}")
            current = raw.execute("SELECT current_schema()").fetchone()[0]
            if current != schema:
                raw.close()
                raise RuntimeError(f"Database didn't switch to schema {schema!r}; refusing to continue.")
            raw.commit()
        conn = Database(raw, "postgres")
    else:
        raw = sqlite3.connect(target, check_same_thread=False)
        raw.row_factory = _sqlite_row_factory
        conn = Database(raw, "sqlite")
    for statement in TABLES:
        conn.execute(statement)
    _migrate(conn)
    return conn


def _migrate(conn: Database) -> None:
    for table, cols in ADDED_COLUMNS.items():
        if conn.kind == "postgres":
            for name, decl in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {decl}")
        else:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def patch_of(game_version: str) -> str:
    parts = game_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


def _csv(values) -> str:
    return ",".join(str(v) for v in values)


def _runes(p: dict) -> tuple[str, str, str]:
    perks = p.get("perks") or {}
    styles = perks.get("styles") or []
    picks = [s.get("perk") for st in styles for s in st.get("selections", []) if s.get("perk")]
    style_ids = [st.get("style") for st in styles if st.get("style")]
    sp = perks.get("statPerks") or {}
    shards = [sp.get("offense"), sp.get("flex"), sp.get("defense")]
    return _csv(picks), _csv(style_ids), _csv(x for x in shards if x)


def store_match(conn: Database, match: dict) -> bool:
    """Save a match-v5 payload. Returns False for remakes or games without roles."""
    info = match.get("info", {})
    if info.get("gameDuration", 0) < 300:
        return False
    parts = info.get("participants", [])
    if len(parts) != 10 or any(not p.get("teamPosition") for p in parts):
        return False
    mid = match["metadata"]["matchId"]
    conn.execute("""INSERT INTO matches (match_id, patch, queue_id, game_start, duration, has_builds)
                    VALUES (?,?,?,?,?,1)
                    ON CONFLICT (match_id) DO UPDATE SET has_builds = 1""",
                 (mid, patch_of(info.get("gameVersion", "")), info.get("queueId"),
                  info.get("gameStartTimestamp"), info.get("gameDuration")))
    update = ", ".join(f"{c} = excluded.{c}" for c in PARTICIPANT_COLS[2:])
    sql = (f"INSERT INTO participants ({', '.join(PARTICIPANT_COLS)}) "
           f"VALUES ({','.join('?' * len(PARTICIPANT_COLS))}) "
           f"ON CONFLICT (match_id, team_id, position) DO UPDATE SET {update}")
    for p in parts:
        runes, styles, shards = _runes(p)
        items = [p.get(f"item{i}", 0) for i in range(6)]
        conn.execute(sql, (
            mid, p["teamId"], p["championId"], p["teamPosition"], int(bool(p["win"])),
            p.get("physicalDamageDealtToChampions", 0), p.get("magicDamageDealtToChampions", 0),
            p.get("trueDamageDealtToChampions", 0), p.get("totalDamageTaken", 0),
            p.get("timeCCingOthers", 0), _csv(i for i in items if i),
            _csv(sorted(x for x in (p.get("summoner1Id"), p.get("summoner2Id")) if x)),
            runes, styles, shards, p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0),
            p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)))
    for team in info.get("teams", []):
        for b in team.get("bans", []):
            if b.get("championId", -1) > 0:
                conn.execute("INSERT INTO bans VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                             (mid, team["teamId"], b.get("pickTurn", 0), b["championId"]))
    return True


def store_timeline(conn: Database, match: dict, timeline: dict,
                   is_completed_item=lambda item_id: True) -> bool:
    """Skill order, starting items and completed-item order from a match timeline."""
    match_id = match["metadata"]["matchId"]
    frames = (timeline.get("info") or {}).get("frames") or []
    # participantId -> (team, position), taken from the match itself
    pid_map = {p["participantId"]: (p["teamId"], p.get("teamPosition"))
               for p in match.get("info", {}).get("participants", []) if p.get("participantId")}
    skills = {pid: [] for pid in pid_map}
    start = {pid: [] for pid in pid_map}
    order = {pid: [] for pid in pid_map}
    for frame in frames:
        for ev in frame.get("events", []):
            pid = ev.get("participantId")
            if pid not in skills:
                continue
            if ev.get("type") == "SKILL_LEVEL_UP" and ev.get("levelUpType", "NORMAL") == "NORMAL":
                skills[pid].append("QWER"[ev.get("skillSlot", 1) - 1])
            elif ev.get("type") == "ITEM_PURCHASED":
                if ev.get("timestamp", 0) <= 90_000:
                    start[pid].append(ev["itemId"])
                elif is_completed_item(ev["itemId"]) and ev["itemId"] not in order[pid]:
                    order[pid].append(ev["itemId"])
    if not frames or len(pid_map) != 10:
        return False
    for pid, (team, pos) in pid_map.items():
        conn.execute("""UPDATE participants SET skill_order = ?, start_items = ?, item_order = ?
                        WHERE match_id = ? AND team_id = ? AND position = ?""",
                     ("".join(skills[pid][:18]), _csv(start[pid]), _csv(order[pid][:6]),
                      match_id, team, pos))
    conn.execute("UPDATE matches SET has_timeline = 1 WHERE match_id = ?", (match_id,))
    return True


def has_match(conn: Database, match_id: str) -> bool:
    """True when the match is stored with build data (older rows get fetched again)."""
    row = conn.execute("SELECT has_builds FROM matches WHERE match_id=?", (match_id,)).fetchone()
    return bool(row and row[0])


def _patch_key(p: str):
    try:
        return tuple(int(x) for x in p.split("."))
    except ValueError:
        return (0,)


def _delete_matches(conn: Database, where: str, args: list) -> int:
    keep_mine = "match_id NOT IN (SELECT match_id FROM my_games)"
    ids = [r[0] for r in conn.execute(f"SELECT match_id FROM matches WHERE {where} AND {keep_mine}", args)]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for table in ("participants", "bans", "matches"):
            conn.execute(f"DELETE FROM {table} WHERE match_id IN ({marks})", chunk)
    conn.commit()
    return len(ids)


def prune(conn: Database, keep_patches: int = 2, max_matches: int = 55000) -> dict:
    """Keep the database small: drop games from old patches, then the oldest
    games if there are still more than max_matches. Your own games are kept."""
    patches = sorted({r[0] for r in conn.execute("SELECT DISTINCT patch FROM matches")}, key=_patch_key, reverse=True)
    old = patches[keep_patches:]
    dropped_old = _delete_matches(conn, f"patch IN ({','.join('?' * len(old))})", old) if old else 0
    total = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    dropped_extra = 0
    if total > max_matches:
        cutoff = conn.execute("SELECT game_start FROM matches ORDER BY game_start DESC LIMIT 1 OFFSET ?",
                              (max_matches,)).fetchone()
        if cutoff:
            dropped_extra = _delete_matches(conn, "game_start <= ?", [cutoff[0]])
    return {"old_patches": dropped_old, "over_limit": dropped_extra,
            "left": conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]}


def add_players(conn: Database, match: dict, tier: str | None = None) -> None:
    """Everyone in a crawled game becomes someone to crawl later (they're at a similar rank)."""
    for p in match.get("info", {}).get("participants", []):
        if p.get("puuid") and p["puuid"] != "BOT":
            conn.execute("INSERT INTO players (puuid, tier) VALUES (?,?) ON CONFLICT DO NOTHING", (p["puuid"], tier))
