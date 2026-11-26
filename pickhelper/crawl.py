"""Collect ranked solo queue matches to build matchup and synergy stats.

Flow: ladder players at the chosen tiers -> their recent ranked match IDs ->
full match details. Already-stored matches are skipped, so running it again
only adds new games.
"""
from __future__ import annotations

import random
import time

from . import db
from .riot import RiotClient, RiotError
from .db import Database

APEX = ("MASTER", "GRANDMASTER", "CHALLENGER")
DIVISIONS = ("I", "II", "III", "IV")


def seed_players(client: RiotClient, conn: Database, tiers: list[str],
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
                    cur = conn.execute("INSERT INTO players (puuid, tier) VALUES (?,?) ON CONFLICT DO NOTHING",
                                       (puuid, tier))
                    added += cur.rowcount
        conn.commit()
        log(f"  seeded {tier}: {added} new players so far")
    return added


def crawl(client: RiotClient, conn: Database, max_requests: int = 1000,
          matches_per_player: int = 10, log=print, timelines: bool = False,
          is_completed_item=lambda item_id: True) -> int:
    """Fetch new matches until the request budget runs out. Returns matches stored.

    With timelines=True each match costs a second request, which adds skill
    order, starting items and build order but halves how many games you get.
    """
    start_requests = client.requests_made
    stored = 0

    # Matches saved before item and rune data was kept: download them again first.
    stale = [r[0] for r in conn.execute("SELECT match_id FROM matches WHERE has_builds = 0")]
    if stale:
        log(f"  updating {len(stale)} older matches with build data")
    for mid in stale:
        if client.requests_made - start_requests >= max_requests:
            conn.commit()
            return stored
        try:
            match = client.match(mid)
            if db.store_match(conn, match):
                stored += 1
                if timelines:
                    db.store_timeline(conn, match, client.timeline(mid), is_completed_item)
            else:
                conn.execute("DELETE FROM matches WHERE match_id = ?", (mid,))
        except RiotError as e:
            log(f"  skipped {mid}: {e}")
        if stored and stored % 50 == 0:
            conn.commit()
            log(f"  {stored} matches updated, {client.requests_made - start_requests} requests used")
    conn.commit()

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
                match = client.match(mid)
                if db.store_match(conn, match):
                    stored += 1
                    if timelines:
                        db.store_timeline(conn, match, client.timeline(mid), is_completed_item)
            except RiotError as e:
                log(f"  skipped {mid}: {e}")
        conn.commit()
        log(f"  {stored} new matches, {client.requests_made - start_requests} requests used")
    return stored
