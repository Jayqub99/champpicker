"""Your own account: mastery and recent ranked games, for the comfort score."""
from __future__ import annotations


from . import db
from .riot import RiotClient, RiotError
from .db import Database


def sync(client: RiotClient, conn: Database, riot_id: str, games: int = 100,
         log=print) -> dict:
    account = client.account_by_riot_id(riot_id)
    puuid = account["puuid"]

    masteries = client.masteries(puuid)
    conn.execute("DELETE FROM my_mastery")
    conn.executemany("INSERT INTO my_mastery VALUES (?,?,?)",
                     [(m["championId"], m.get("championPoints", 0), m.get("lastPlayTime", 0))
                      for m in masteries])

    ids: list[str] = []
    for queue in (420, 440):  # ranked solo/duo, ranked flex
        start = 0
        while start < games:
            batch = client.match_ids(puuid, count=min(100, games - start), start=start, queue=queue)
            ids += batch
            if len(batch) < 100:
                break
            start += 100

    added = 0
    known = {r[0] for r in conn.execute("SELECT match_id FROM my_games")}
    for mid in ids:
        if mid in known:
            continue
        try:
            match = client.match(mid)
        except RiotError as e:
            log(f"  skipped {mid}: {e}")
            continue
        db.store_match(conn, match)  # your games also feed the general stats
        me = next((p for p in match["info"]["participants"] if p.get("puuid") == puuid), None)
        if me and match["info"].get("gameDuration", 0) >= 300:
            conn.execute("INSERT INTO my_games VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                         (mid, me["championId"], me.get("teamPosition") or "", int(bool(me["win"])),
                          match["info"].get("gameStartTimestamp")))
            added += 1
    conn.commit()
    log(f"  {len(masteries)} champions with mastery, {added} new games of yours")
    return {"puuid_found": True, "games_added": added, "mastery_rows": len(masteries)}
