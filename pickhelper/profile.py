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

    try:
        summ = client.summoner_by_puuid(puuid)
    except Exception:  # the icon and level are nice to have, not essential
        summ = {}

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
            "trinket": game.item(me["item6"]) if me.get("item6") else None,
            "spells": [game.spell(me.get(k)) for k in ("summoner1Id", "summoner2Id") if me.get(k)],
            "keystone": _keystone(game, me),
            "secondary": _secondary(game, me),
            "kda_ratio": round((me.get("kills", 0) + me.get("assists", 0)) / max(1, me.get("deaths", 0)), 2),
            "kill_share": _kill_share(info, me),
            "vision": me.get("visionScore", 0),
            "level": me.get("champLevel", 0),
            "teams": [[{"name": game.name(p.get("championId", 0)),
                        "image": game.by_id.get(p.get("championId", 0), {}).get("image"),
                        "player": p.get("riotIdGameName") or p.get("summonerName") or "",
                        "me": p.get("puuid") == puuid}
                       for p in info.get("participants", []) if p.get("teamId") == team]
                      for team in (100, 200)],
        })
    conn.commit()

    played = [g for g in games if not g["remake"]]
    by_champ: dict[str, list[bool]] = {}
    for g in played:
        by_champ.setdefault(g["champion"], []).append(g["win"])
    images = {g["champion"]: g["image"] for g in played}
    summary = sorted(({"champion": c, "games": len(r), "wins": sum(r), "image": images.get(c)}
                      for c, r in by_champ.items()),
                     key=lambda x: -x["games"])
    result = {"riot_id": f"{account.get('gameName', '')}#{account.get('tagLine', '')}",
              "icon": summ.get("profileIconId"), "level": summ.get("summonerLevel"),
              "ranks": ranks, "games": games, "champions": summary,
              "recent_wins": sum(g["win"] for g in played), "recent_games": len(played)}
    _cache[key] = (time.time(), result)
    return result


def _perk_styles(me: dict) -> list[dict]:
    return (me.get("perks") or {}).get("styles") or []


def _keystone(game: Champions, me: dict) -> dict | None:
    styles = _perk_styles(me)
    sel = styles[0].get("selections") if styles else None
    return game.rune(sel[0]["perk"]) if sel else None


def _secondary(game: Champions, me: dict) -> dict | None:
    styles = _perk_styles(me)
    return game.rune(styles[1]["style"]) if len(styles) > 1 and styles[1].get("style") else None


def _kill_share(info: dict, me: dict) -> int:
    team_kills = sum(p.get("kills", 0) for p in info.get("participants", []) if p.get("teamId") == me.get("teamId"))
    return round(100 * (me.get("kills", 0) + me.get("assists", 0)) / team_kills) if team_kills else 0
