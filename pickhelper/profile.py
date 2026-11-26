"""Player lookup: rank and recent games for any Riot ID (about 12 requests each)."""
from __future__ import annotations

import time

from . import db
from .ddragon import Champions
from .riot import RiotClient
from .db import Database

QUEUES = {420: "Ranked Solo", 440: "Ranked Flex", 400: "Normal Draft", 430: "Normal Blind",
          450: "ARAM", 490: "Quickplay", 1700: "Arena", 1900: "URF"}

_cache: dict[str, tuple[float, dict]] = {}
CACHE_SECONDS = 120


def lookup(client: RiotClient, conn: Database, riot_id: str, game: Champions,
           count: int = 10) -> dict:
    key = riot_id.strip().lower()
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    account = client.account_by_riot_id(riot_id.strip())
    puuid = account["puuid"]
    ranks = [{"queue": "Ranked Solo" if r.get("queueType") == "RANKED_SOLO_5x5" else "Ranked Flex",
              "tier": r.get("tier", "").title(), "division": r.get("rank", ""),
              "lp": r.get("leaguePoints", 0), "wins": r.get("wins", 0), "losses": r.get("losses", 0)}
             for r in client.ranks(puuid) if r.get("queueType") in ("RANKED_SOLO_5x5", "RANKED_FLEX_SR")]

    games = []
    for mid in client.match_ids(puuid, count=count, queue=None):
        match = client.match(mid)
        db.store_match(conn, match)  # ranked games also feed the stats
        info = match.get("info", {})
        me = next((p for p in info.get("participants", []) if p.get("puuid") == puuid), None)
        if not me:
            continue
        minutes = max(1, info.get("gameDuration", 0) / 60)
        cs = me.get("totalMinionsKilled", 0) + me.get("neutralMinionsKilled", 0)
        games.append({
            "queue": QUEUES.get(info.get("queueId"), "Other"),
            "champion": game.name(me["championId"]),
            "image": game.by_id.get(me["championId"], {}).get("image"),
            "position": me.get("teamPosition") or "",
            "win": bool(me.get("win")), "remake": info.get("gameDuration", 0) < 300,
            "kda": f"{me.get('kills', 0)}/{me.get('deaths', 0)}/{me.get('assists', 0)}",
            "cs": cs, "cs_per_min": round(cs / minutes, 1),
            "minutes": round(minutes), "started": info.get("gameStartTimestamp"),
            "items": [game.item(me.get(f"item{i}", 0)) for i in range(6) if me.get(f"item{i}")],
        })
    conn.commit()

    played = [g for g in games if not g["remake"]]
    by_champ: dict[str, list[bool]] = {}
    for g in played:
        by_champ.setdefault(g["champion"], []).append(g["win"])
    summary = sorted(({"champion": c, "games": len(r), "wins": sum(r)} for c, r in by_champ.items()),
                     key=lambda x: -x["games"])
    result = {"riot_id": f"{account.get('gameName', '')}#{account.get('tagLine', '')}",
              "ranks": ranks, "games": games, "champions": summary,
              "recent_wins": sum(g["win"] for g in played), "recent_games": len(played)}
    _cache[key] = (time.time(), result)
    return result
