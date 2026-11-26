"""Collect ranked solo queue matches to build matchup and synergy stats.

Flow: ladder players at the chosen tiers -> their recent ranked match IDs ->
full match details. Already-stored matches are skipped, so running it again
only adds new games.
"""
from __future__ import annotations

import random
import sqlite3
import time

from . import db
from .riot import RiotClient, RiotError

APEX = ("MASTER", "GRANDMASTER", "CHALLENGER")
DIVISIONS = ("I", "II", "III", "IV")


def seed_players(client: RiotClient, conn: sqlite3.Connection, tiers: list[str],
                 pages_per_division: int = 1, log=print) -> int:
    added = 0
    for tier in tiers:
        tier = tier.upper()
        if tier in APEX:
            batches = [client.apex_league(tier)]
        else:
            batches = [client.league_entries(tier, div, page)
                       for div in DIVISIONS for page in range(1, pages_per_division + 1)]
        for entries in batches:
            for e in entries:
                puuid = e.get("puuid")
                if not puuid and e.get("summonerId"):
                    try:
                        puuid = client.summoner(e["summonerId"]).get("puuid")
                    except RiotError:
                        continue
                if puuid:
                    cur = conn.execute("INSERT OR IGNORE INTO players (puuid, tier) VALUES (?,?)",
                                       (puuid, tier))
                    added += cur.rowcount
        conn.commit()
        log(f"  seeded {tier}: {added} new players so far")
    return added


def crawl(client: RiotClient, conn: sqlite3.Connection, max_requests: int = 1000,
          matches_per_player: int = 10, log=print) -> int:
    """Fetch new matches until the request budget runs out. Returns matches stored."""
    start_requests = client.requests_made
    stored = 0
    players = [r["puuid"] for r in conn.execute(
        "SELECT puuid FROM players ORDER BY last_crawled ASC LIMIT 500")]
    random.shuffle(players)
    for puuid in players:
        if client.requests_made - start_requests >= max_requests:
            break
        try:
            ids = client.match_ids(puuid, count=matches_per_player)
        except RiotError as e:
            log(f"  skipped a player: {e}")
            continue
        conn.execute("UPDATE players SET last_crawled=? WHERE puuid=?", (int(time.time()), puuid))
        for mid in ids:
            if client.requests_made - start_requests >= max_requests:
                break
            if db.has_match(conn, mid):
                continue
            try:
                if db.store_match(conn, client.match(mid)):
                    stored += 1
            except RiotError as e:
                log(f"  skipped {mid}: {e}")
        conn.commit()
        log(f"  {stored} new matches, {client.requests_made - start_requests} requests used")
    return stored
