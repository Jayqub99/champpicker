"""Command line: python -m pickhelper <command>

  sync-me    pull your mastery and recent ranked games
  seed       collect ladder players to crawl from
  crawl      download matches (respects personal-key rate limits)
  serve      open the local app with live champ-select picks
  recommend  one-off recommendation from the terminal
  companion  send champ select to your hosted site while you play
"""
from __future__ import annotations

import argparse

from . import companion, config, crawl, db, ddragon, engine, me
from .riot import RiotClient, RiotError
from .stats import Stats


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pickhelper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync-me")
    s = sub.add_parser("seed")
    s.add_argument("--pages", type=int, default=1, help="ladder pages per division (205 players each)")
    s.add_argument("--if-empty", action="store_true", help="skip when players are already saved")
    c = sub.add_parser("crawl")
    c.add_argument("--max-requests", type=int, default=1000)
    c.add_argument("--per-player", type=int, default=10)
    c.add_argument("--timelines", action="store_true",
                   help="also fetch skill order, starting items and build order (half as many games)")
    c.add_argument("--timeline-every", type=int, default=0, metavar="N",
                   help="fetch timelines for every Nth match only (cheaper than --timelines)")
    pr = sub.add_parser("prune", help="drop old-patch games so the database stays small")
    pr.add_argument("--keep-patches", type=int, default=2)
    pr.add_argument("--max-matches", type=int, default=55000)
    v = sub.add_parser("serve")
    v.add_argument("--port", type=int, default=8777)
    v.add_argument("--no-watch", action="store_true", help="don't watch the League client")
    r = sub.add_parser("recommend")
    r.add_argument("--role")
    r.add_argument("--allies", default="")
    r.add_argument("--enemies", default="")
    r.add_argument("--bans", default="")
    sub.add_parser("companion")
    args = ap.parse_args(argv)

    cfg = config.load()
    if args.cmd == "companion":
        if not cfg.site_url or not cfg.site_password:
            raise SystemExit("Set site_url and site_password in config.json first.")
        companion.run(cfg.site_url, cfg.site_password)
        return
    conn = db.connect(cfg.db_target)

    if args.cmd == "prune":
        r = db.prune(conn, args.keep_patches, args.max_matches)
        print(f"Dropped {r['old_patches']} old-patch games and {r['over_limit']} over the limit; {r['left']} left.")
        return

    if args.cmd in ("sync-me", "seed", "crawl"):
        try:
            client = RiotClient(cfg.api_key, cfg.platform, cfg.region)
        except ValueError as e:
            raise SystemExit(str(e))
        try:
            if args.cmd == "sync-me":
                if not cfg.riot_id:
                    raise SystemExit("Set riot_id in config.json first.")
                me.sync(client, conn, cfg.riot_id)
            elif args.cmd == "seed":
                if args.if_empty and conn.execute("SELECT 1 FROM players LIMIT 1").fetchone():
                    print("Players already saved; skipping seed.")
                else:
                    crawl.seed_players(client, conn, cfg.crawl_tiers, args.pages)
            else:
                if not conn.execute("SELECT 1 FROM players LIMIT 1").fetchone():
                    raise SystemExit("No players to crawl yet. Run: python -m pickhelper seed")
                timelines = True if args.timelines else args.timeline_every
                items = ddragon.load(config.DATA_DIR).is_completed if timelines else (lambda i: True)
                n = crawl.crawl(client, conn, args.max_requests, args.per_player,
                                timelines=timelines, is_completed_item=items)
                print(f"Done: {n} new matches.")
        except RiotError as e:
            raise SystemExit(f"{e}\n{e.hint()}".strip())
        return

    champs = ddragon.load(config.DATA_DIR)
    if args.cmd == "serve":
        from .server import serve
        serve(cfg, champs, args.port, watch=not args.no_watch)
        return

    role = (args.role or cfg.role).upper()
    find = lambda raw: [x for x in (champs.find(t) for t in raw.split(",") if t.strip()) if x]
    stats = Stats(conn, cfg.patches_to_use)
    fixed = [x for x in (champs.find(n) for n in cfg.pool) if x] if role == cfg.role else None
    picks = engine.recommend(stats, role, [engine.Ally(a) for a in find(args.allies)], find(args.enemies),
                             find(args.bans), engine.candidate_pool(stats, role, fixed),
                             names=champs.name, top=5)
    if not picks:
        print("No candidates. Run sync-me and crawl first, or set a pool in config.json.")
    for i, p in enumerate(picks, 1):
        flag = "  (little data)" if p.games_in_role < 50 else ""
        print(f"{i}. {p.name}: about {p.win_pct:.1f}% win chance{flag}")
        for reason in p.reasons:
            print(f"     {reason}")


if __name__ == "__main__":
    main()
