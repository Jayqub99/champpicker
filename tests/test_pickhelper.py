import io
import itertools
import json
import os
import random
import sys
import tempfile
import threading
import unittest
import zlib
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pickhelper import builds, db, engine, lcu, profile  # noqa: E402
from pickhelper.config import Config  # noqa: E402
from pickhelper.ddragon import Champions  # noqa: E402
from pickhelper.riot import RateLimiter, RiotClient, RiotError  # noqa: E402
from pickhelper.stats import Stats  # noqa: E402

# Set TEST_DATABASE_URL to a Postgres URL to run everything against Postgres too.
PG_URL = os.environ.get("TEST_DATABASE_URL", "")
_schemas = itertools.count(1)


def db_target() -> str:
    """A fresh empty database: SQLite in memory, or a new Postgres schema."""
    if not PG_URL:
        return ":memory:"
    import psycopg
    schema = f"t{os.getpid()}_{next(_schemas)}"
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.execute(f"CREATE SCHEMA {schema}")
    sep = "&" if "?" in PG_URL else "?"
    return f"{PG_URL}{sep}options=-csearch_path%3D{schema}"


def fresh_db():
    return db.connect(db_target())


# Tiny fake champion list: id -> (name, usual role)
CHAMPS = {1: ("Jinx", "BOTTOM"), 2: ("Caitlyn", "BOTTOM"), 3: ("Ezreal", "BOTTOM"),
          4: ("Draven", "BOTTOM"), 5: ("Thresh", "UTILITY"), 6: ("Lux", "UTILITY"),
          7: ("Darius", "TOP"), 8: ("Ahri", "MIDDLE"), 9: ("LeeSin", "JUNGLE"),
          10: ("Garen", "TOP"), 11: ("Zed", "MIDDLE"), 12: ("Vi", "JUNGLE"),
          13: ("Nautilus", "UTILITY")}
POS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


ITEMS = {"3006": ("Berserker's Greaves", 1100, ["Boots"], []), "6672": ("Kraken Slayer", 3100, [], []),
         "3031": ("Infinity Edge", 3450, [], []), "3094": ("Rapid Firecannon", 2650, [], []),
         "3036": ("Lord Dominik's Regards", 3000, [], []), "1055": ("Doran's Blade", 450, [], ["3031"]),
         "2003": ("Health Potion", 50, ["Consumable"], [])}
RUNES = [{"id": 8000, "name": "Precision", "icon": "p.png", "slots": [
             {"runes": [{"id": 8008, "name": "Lethal Tempo", "icon": "lt.png"}]},
             {"runes": [{"id": 9111, "name": "Triumph", "icon": "t.png"}]},
             {"runes": [{"id": 9104, "name": "Legend: Alacrity", "icon": "a.png"}]},
             {"runes": [{"id": 8014, "name": "Coup de Grace", "icon": "c.png"}]}]},
         {"id": 8100, "name": "Domination", "icon": "d.png", "slots": [
             {"runes": [{"id": 8139, "name": "Taste of Blood", "icon": "tb.png"},
                        {"id": 8135, "name": "Treasure Hunter", "icon": "th.png"}]}]}]


def champions():
    data = {"data": {n: {"key": str(i), "name": n, "id": n, "tags": [], "image": {"full": n + ".png"}}
                     for i, (n, _) in CHAMPS.items()}}
    items = {"data": {k: {"name": n, "gold": {"total": g}, "tags": t, "into": into, "image": {"full": k + ".png"},
                          "maps": {"11": True}} for k, (n, g, t, into) in ITEMS.items()}}
    spells = {"data": {"SummonerFlash": {"key": "4", "name": "Flash", "image": {"full": "f.png"}},
                       "SummonerHeal": {"key": "7", "name": "Heal", "image": {"full": "h.png"}}}}
    return Champions("1.0.1", data, items, spells, RUNES)


PAGE = {"styles": [{"style": 8000, "selections": [{"perk": 8008}, {"perk": 9111}, {"perk": 9104}, {"perk": 8014}]},
                   {"style": 8100, "selections": [{"perk": 8139}, {"perk": 8135}]}],
        "statPerks": {"offense": 5005, "flex": 5008, "defense": 5011}}


def fake_match(mid, picks, blue_wins, patch="15.23"):
    parts = []
    for team, champs in ((100, picks[0]), (200, picks[1])):
        for pos, c in zip(POS, champs):
            magic = 20000 if CHAMPS[c][0] in ("Lux", "Ahri") else 1000
            win = (team == 100) == blue_wins
            # Winning Jinx games lean Infinity Edge; losing ones lean Lord Dominik's.
            third = 3031 if (win and zlib.crc32(mid.encode()) % 4) or (not win and zlib.crc32(mid.encode()) % 4 == 0) else 3036
            items = [3006, 6672, third, 3094, 2003, 0]
            parts.append({"teamId": team, "championId": c, "teamPosition": pos, "participantId": len(parts) + 1,
                          "win": win, "puuid": f"p{team}{pos}",
                          "physicalDamageDealtToChampions": 15000, "magicDamageDealtToChampions": magic,
                          "trueDamageDealtToChampions": 500, "totalDamageTaken": 20000,
                          "timeCCingOthers": 10, "summoner1Id": 7, "summoner2Id": 4, "perks": PAGE,
                          "kills": 5, "deaths": 2, "assists": 7, "totalMinionsKilled": 200, "neutralMinionsKilled": 10,
                          **{f"item{i}": it for i, it in enumerate(items)}})
    return {"metadata": {"matchId": mid},
            "info": {"gameDuration": 1800, "gameVersion": f"{patch}.1.1", "queueId": 420,
                     "gameStartTimestamp": 0, "participants": parts,
                     "teams": [{"teamId": 100, "bans": [{"championId": 11, "pickTurn": 1}]},
                               {"teamId": 200, "bans": [{"championId": -1, "pickTurn": 6}]}]}}


def build_db(conn, n=3000, seed=7):
    """Jinx crushes Draven in lane; Caitlyn loses to Draven; Ezreal is neutral."""
    rnd = random.Random(seed)
    for i in range(n):
        blue_adc, red_adc = rnd.choice([1, 2, 3]), 4
        p = 0.5 + {1: 0.12, 2: -0.10, 3: 0.0}[blue_adc]
        blue = [rnd.choice([7, 10]), rnd.choice([9, 12]), rnd.choice([8, 11]), blue_adc, rnd.choice([5, 6])]
        red = [rnd.choice([7, 10]), rnd.choice([9, 12]), rnd.choice([8, 11]), red_adc, 13]
        db.store_match(conn, fake_match(f"NA1_{i}", (blue, red), rnd.random() < p))
    conn.commit()


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = fresh_db()
        build_db(cls.conn)
        cls.stats = Stats(cls.conn, 2)
        cls.names = champions().name

    def test_counter_pick_ranks_first(self):
        picks = engine.recommend(self.stats, "BOTTOM", [engine.Ally(5, "UTILITY")], [4], [], [1, 2, 3],
                                 names=self.names)
        self.assertEqual([p.name for p in picks], ["Jinx", "Ezreal", "Caitlyn"])
        self.assertTrue(any("Draven" in r for r in picks[0].reasons))
        self.assertGreater(picks[0].win_pct, 55)

    def test_taken_and_banned_are_excluded(self):
        picks = engine.recommend(self.stats, "BOTTOM", [], [4], [1], [1, 2, 3], names=self.names)
        self.assertNotIn("Jinx", [p.name for p in picks])

    def test_hovered_ally_is_not_excluded(self):
        picks = engine.recommend(self.stats, "BOTTOM", [engine.Ally(1, "TOP", hover=True)], [4], [],
                                 [1, 2], names=self.names)
        self.assertIn("Jinx", [p.name for p in picks])

    def test_comfort_moves_ranking(self):
        conn = fresh_db()
        build_db(conn, n=600)
        for i in range(30):  # 30 Caitlyn games, 24 won
            conn.execute("INSERT INTO my_games VALUES (?,?,?,?,?)", (f"me{i}", 2, "BOTTOM", int(i < 24), 0))
        stats = Stats(conn, 2)
        no_enemy = engine.recommend(stats, "BOTTOM", [], [], [], [2, 3], names=self.names)
        self.assertEqual(no_enemy[0].name, "Caitlyn")

    def test_pool_from_history(self):
        conn = fresh_db()
        build_db(conn, n=300)
        conn.execute("INSERT INTO my_games VALUES ('a',3,'BOTTOM',1,0)")
        conn.execute("INSERT INTO my_games VALUES ('b',3,'BOTTOM',0,0)")
        conn.execute("INSERT INTO my_games VALUES ('c',2,'BOTTOM',1,0)")
        self.assertEqual(engine.candidate_pool(Stats(conn, 2), "BOTTOM"), [3])

    def test_empty_database_is_safe(self):
        stats = Stats(fresh_db(), 2)
        self.assertEqual(stats.patches, [])
        picks = engine.recommend(stats, "BOTTOM", [], [4], [], [1, 2], names=self.names)
        self.assertEqual(len(picks), 2)
        self.assertAlmostEqual(picks[0].win_pct, picks[1].win_pct, delta=0.01)

    def test_only_recent_patches_count(self):
        conn = fresh_db()
        build_db(conn, n=200)
        db.store_match(conn, fake_match("old", ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), True, patch="14.1"))
        self.assertEqual(Stats(conn, 1).patches, ["15.23"])
        self.assertEqual(Stats(conn, 5).patches, ["15.23", "14.1"])


class StoreTests(unittest.TestCase):
    def test_remake_and_missing_roles_skipped(self):
        conn = fresh_db()
        m = fake_match("x", ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), True)
        m["info"]["gameDuration"] = 200
        self.assertFalse(db.store_match(conn, m))
        m = fake_match("y", ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), True)
        m["info"]["participants"][0]["teamPosition"] = ""
        self.assertFalse(db.store_match(conn, m))
        self.assertEqual(db.patch_of("15.23.712.3456"), "15.23")


class RateLimiterTests(unittest.TestCase):
    def test_waits_when_window_full(self):
        t = [0.0]
        slept = []

        def sleep(s):
            slept.append(s)
            t[0] += s
        rl = RateLimiter(((3, 1.0), (5, 10.0)), clock=lambda: t[0], sleep=sleep)
        for _ in range(5):
            rl.acquire()
        self.assertGreaterEqual(t[0], 1.0)      # 4th call waited for the 1-second window
        rl.acquire()
        self.assertGreaterEqual(t[0], 10.0)     # 6th call waited for the 10-second window


class RiotClientTests(unittest.TestCase):
    def test_key_goes_in_header_and_404_raises(self):
        seen = {}

        def opener(req, timeout):
            seen["url"], seen["key"] = req.full_url, req.get_header("X-riot-token")
            seen["ua"] = req.get_header("User-agent")
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"status":"nf"}'))
        c = RiotClient("RGAPI-test", opener=opener)
        with self.assertRaises(RiotError) as ctx:
            c.account_by_riot_id("Some Name#NA1")
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(seen["key"], "RGAPI-test")
        self.assertTrue(seen["ua"].startswith("champpicker/"))  # Cloudflare blocks Python-urllib
        self.assertNotIn("RGAPI", seen["url"])
        self.assertIn("americas.api.riotgames.com/riot/account/v1/accounts/by-riot-id/Some%20Name/NA1",
                      seen["url"])

    def test_error_hints(self):
        self.assertIn("git pull", RiotError(403, "error code: 1010").hint())
        self.assertIn("expire", RiotError(403, '{"status":{"message":"Forbidden"}}').hint())
        self.assertIn("riot_id", RiotError(404, "nf").hint())

    def test_ddragon_sends_user_agent(self):
        from pickhelper import ddragon
        seen = {}
        orig = ddragon.urllib.request.urlopen
        ddragon.urllib.request.urlopen = lambda req, timeout: seen.setdefault("ua", req.get_header("User-agent"))
        try:
            ddragon._get("https://example.invalid/x", 5)
        finally:
            ddragon.urllib.request.urlopen = orig
        self.assertTrue(seen["ua"].startswith("champpicker/"))

    def test_requires_key(self):
        with self.assertRaises(ValueError):
            RiotClient("")


SESSION = {
    "localPlayerCellId": 3,
    "timer": {"phase": "BAN_PICK"},
    "myTeam": [
        {"cellId": 0, "championId": 7, "assignedPosition": "top"},
        {"cellId": 1, "championId": 0, "championPickIntent": 9, "assignedPosition": "jungle"},
        {"cellId": 2, "championId": 0, "championPickIntent": 0, "assignedPosition": "middle"},
        {"cellId": 3, "championId": 0, "championPickIntent": 1, "assignedPosition": "bottom"},
        {"cellId": 4, "championId": 5, "assignedPosition": "utility"},
    ],
    "theirTeam": [{"cellId": 5, "championId": 4}, {"cellId": 6, "championId": 0}],
    "bans": {"myTeamBans": [11], "theirTeamBans": [0]},
    "actions": [[{"type": "ban", "completed": True, "championId": 12, "isAllyAction": False}],
                [{"type": "pick", "completed": True, "championId": 13, "isAllyAction": False}]],
}


class LcuTests(unittest.TestCase):
    def test_parse_session(self):
        cs = lcu.parse_session(SESSION)
        self.assertEqual(cs.my_position, "BOTTOM")
        self.assertEqual(cs.my_champion, 1)
        self.assertEqual([(a.champion_id, a.position, a.hover) for a in cs.allies],
                         [(7, "TOP", False), (9, "JUNGLE", True), (5, "UTILITY", False)])
        self.assertEqual(cs.enemies, [4, 13])
        self.assertEqual(cs.bans, [11, 12])

    def test_credentials(self):
        self.assertEqual(lcu.parse_lockfile("LeagueClient:1234:50123:abcDEF:https").port, 50123)
        creds = lcu.parse_cmdline('"LeagueClientUx.exe" "--remoting-auth-token=xY_z-1" "--app-port=61234"')
        self.assertEqual((creds.port, creds.password), (61234, "xY_z-1"))
        self.assertIsNone(lcu.parse_cmdline("notepad.exe"))


class ServerTests(unittest.TestCase):
    def test_endpoints(self):
        from pickhelper import server
        from http.server import ThreadingHTTPServer
        target = db_target() if PG_URL else str(Path(tempfile.mkdtemp()) / "x.sqlite3")
        cfg = Config(role="BOTTOM", database_url=target if PG_URL else "")
        if not PG_URL:
            type(cfg).db_path = property(lambda self: Path(target))
        conn = db.connect(target)
        build_db(conn, n=800)
        for r in conn.execute("SELECT match_id, win FROM participants WHERE champion_id = 1 AND position = 'BOTTOM' "
                              "ORDER BY match_id LIMIT 6").fetchall():
            conn.execute("INSERT INTO my_games VALUES (?,?,?,?,?)", (r[0], 1, "BOTTOM", r[1], 0))
        conn.commit()
        conn.close()
        app = server.App(cfg, champions(), watching=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(lambda: app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        get = lambda p: json.loads(urllib.request.urlopen(base + p).read())
        try:
            self.assertEqual(get("/api/status")["matches"], 800)
            me = get("/api/me")
            self.assertEqual(me["summary"]["games"], 6)
            self.assertEqual(me["champions"][0]["name"], "Jinx")
            self.assertEqual(me["champions"][0]["games"], 6)
            self.assertTrue(me["picks"])
            recs = get("/api/recommend?enemies=Draven&allies=Thresh")
            self.assertEqual(recs[0]["name"], "Jinx")
            counters = get("/api/counters?champion=Draven&role=BOTTOM")
            self.assertEqual(counters["worst"][0]["name"], "Jinx")
            tiers = get("/api/tierlist?role=BOTTOM")
            self.assertEqual({t["name"] for t in tiers}, {"Jinx", "Caitlyn", "Ezreal", "Draven"})
            self.assertTrue(all(t["tier"] in ("S+", "S", "A", "B", "C", "D") for t in tiers))
            self.assertTrue(all(t["role"] == "BOTTOM" and len(t["counters"]) <= 3 for t in tiers))
            self.assertTrue(any(t["counters"] for t in tiers))
            every = get("/api/tierlist?role=ALL")
            self.assertGreater(len(every), len(tiers))
            self.assertTrue({"TOP", "BOTTOM"} <= {t["role"] for t in every})
            page = get("/api/champion?champion=Jinx")
            self.assertEqual(page["stats"]["tier"] in ("S+", "S", "A", "B", "C", "D"), True)
            self.assertIn("title", page["champion"])
            self.assertEqual(page["role"], "BOTTOM")
            self.assertEqual(page["duo_role"], "UTILITY")
            self.assertTrue(page["build"]["core"])
            self.assertEqual(get("/api/champion?champion=Zed")["ban_pct"], 100.0)
            self.assertIn("error", get("/api/profile?riot_id=x%23y"))
            self.assertEqual(get("/api/live"), {"connected": False, "in_champ_select": False})
            html = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("Champ Picker", html)
        finally:
            httpd.shutdown()


class HostedTests(unittest.TestCase):
    """The Vercel setup: password, companion drafts, rewritten paths."""

    def setUp(self):
        from pickhelper import server
        from http.server import ThreadingHTTPServer
        target = db_target() if PG_URL else str(Path(tempfile.mkdtemp()) / "h.sqlite3")
        conn = db.connect(target)
        build_db(conn, n=600)
        conn.execute("INSERT INTO my_games VALUES ('m1', 1, 'BOTTOM', 1, 0)")
        conn.execute("INSERT INTO my_games VALUES ('m2', 1, 'BOTTOM', 1, 0)")
        conn.commit()
        conn.close()
        cfg = Config(role="BOTTOM", database_url=target if PG_URL else "", site_password="hunter2")
        if not PG_URL:
            type(cfg).db_path = property(lambda self: Path(target))
        self.app = server.App(cfg, champions(), watching=False)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(lambda: self.app))
        self.httpd.site_password = "hunter2"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, path, data=None, headers=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_password_required(self):
        status, _, body = self.call("/")
        self.assertIn(b"Password", body)
        self.assertEqual(self.call("/api/status")[0], 401)
        self.assertEqual(self.call("/login", b"password=wrong")[0], 401)
        status, headers, _ = self.call("/login", b"password=hunter2")
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, body = self.call("/api/status", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["matches"], 600)
        self.assertEqual(self.call("/api/status", headers={"Authorization": "Bearer hunter2"})[0], 200)
        self.assertEqual(self.call("/api/status", headers={"Cookie": "cp_session=forged"})[0], 401)

    def test_vercel_rewrite_path(self):
        status, _, body = self.call("/api/index?__path=/api/tierlist&role=BOTTOM",
                                    headers={"Authorization": "Bearer hunter2"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body))

    def test_companion_draft_shows_live_picks(self):
        from pickhelper import companion
        auth = {"Authorization": "Bearer hunter2"}
        live = json.loads(self.call("/api/live", headers=auth)[2])
        self.assertEqual(live["connected"], False)
        cs = lcu.parse_session(SESSION)
        sent = []

        def opener(req, timeout):
            sent.append(req)
            return urllib.request.build_opener().open(req, timeout=timeout)
        companion.send(self.base, "hunter2", companion.payload_from(cs), opener=opener)
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer hunter2")
        live = json.loads(self.call("/api/live", headers=auth)[2])
        self.assertTrue(live["in_champ_select"])
        self.assertEqual(live["role"], "BOTTOM")
        self.assertEqual([e["name"] for e in live["draft"]["enemies"]], ["Draven", "Nautilus"])
        self.assertEqual(live["picks"][0]["name"], "Jinx")
        companion.send(self.base, "hunter2", companion.payload_from(None))
        self.assertFalse(json.loads(self.call("/api/live", headers=auth)[2])["in_champ_select"])
        self.assertEqual(self.call("/api/live", b"{}")[0], 401)  # no password, no write


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = fresh_db()
        build_db(cls.conn, n=1200)
        cls.game = champions()

    def test_jinx_build(self):
        b = builds.champion_build(self.conn, ["15.23"], 1, "BOTTOM", self.game)
        self.assertGreater(b["games"], 300)
        self.assertEqual([r["name"] for r in b["runes"]["runes"]][:1], ["Lethal Tempo"])
        self.assertEqual([s["name"] for s in b["runes"]["shards"]], ["Attack Speed", "Adaptive Force", "Health"])
        self.assertEqual(sorted(s["name"] for s in b["spells"]["spells"]), ["Flash", "Heal"])
        core = {i["name"] for i in b["core"]["items"]}
        self.assertEqual(core, {"Kraken Slayer", "Infinity Edge", "Rapid Firecannon"})  # beats the LDR core
        self.assertEqual(b["boots"]["item"]["name"], "Berserker's Greaves")
        names = [i["item"]["name"] for i in b["items"]]
        self.assertNotIn("Health Potion", names)
        self.assertNotIn("Berserker's Greaves", names)
        self.assertNotIn("skills", b)  # no timelines crawled

    def test_timeline_adds_skills_start_and_order(self):
        conn = fresh_db()
        match = fake_match("T1", ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), True)
        db.store_match(conn, match)
        events = [{"type": "ITEM_PURCHASED", "participantId": 4, "itemId": 1055, "timestamp": 20_000},
                  {"type": "ITEM_PURCHASED", "participantId": 4, "itemId": 2003, "timestamp": 21_000}]
        for i, k in enumerate("QWEQQRQWQWRWWEEREE"):
            events.append({"type": "SKILL_LEVEL_UP", "participantId": 4, "skillSlot": "QWER".index(k) + 1,
                           "levelUpType": "NORMAL", "timestamp": 60_000 * (i + 1)})
        for t, item in ((600_000, 6672), (900_000, 3031), (1_200_000, 3094)):
            events.append({"type": "ITEM_PURCHASED", "participantId": 4, "itemId": item, "timestamp": t})
        timeline = {"info": {"frames": [{"events": events}]}}
        self.assertTrue(db.store_timeline(conn, match, timeline, self.game.is_completed))
        row = conn.execute("SELECT * FROM participants WHERE match_id='T1' AND team_id=100 AND position='BOTTOM'").fetchone()
        self.assertEqual(row["skill_order"], "QWEQQRQWQWRWWEEREE")
        self.assertEqual(row["start_items"], "1055,2003")
        self.assertEqual(row["item_order"], "6672,3031,3094")
        self.assertEqual(builds.skill_priority([row["skill_order"]]), "Q > W > E")

    @unittest.skipIf(PG_URL, "SQLite file upgrade only")
    def test_old_database_is_upgraded(self):
        path = Path(tempfile.mkdtemp()) / "old.sqlite3"
        import sqlite3
        old = sqlite3.connect(path)
        old.executescript("""CREATE TABLE matches (match_id TEXT PRIMARY KEY, patch TEXT NOT NULL, queue_id INTEGER,
                              game_start INTEGER, duration INTEGER);
                             INSERT INTO matches VALUES ('NA1_1', '15.23', 420, 0, 1800);""")
        old.commit()
        old.close()
        conn = db.connect(path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
        self.assertIn("has_builds", cols)
        self.assertFalse(db.has_match(conn, "NA1_1"))  # stored without builds, so it gets fetched again
        db.store_match(conn, fake_match("NA1_1", ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), True))
        self.assertTrue(db.has_match(conn, "NA1_1"))
        conn.close()


class FakeRiot:
    requests_made = 0

    def account_by_riot_id(self, rid):
        return {"puuid": "p100BOTTOM", "gameName": "Test", "tagLine": "NA1"}

    def ranks(self, puuid):
        return [{"queueType": "RANKED_SOLO_5x5", "tier": "EMERALD", "rank": "II", "leaguePoints": 40,
                 "wins": 30, "losses": 25}]

    def match_ids(self, puuid, count=10, queue=None, start=0, since=None):
        return ["P1", "P2"]

    def match(self, mid):
        return fake_match(mid, ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), mid == "P1")


class CrawlTests(unittest.TestCase):
    def test_old_matches_are_refetched_first(self):
        from pickhelper import crawl

        class Client(FakeRiot):
            def __init__(self):
                self.requests_made = 0

            def match(self, mid):
                self.requests_made += 1
                return fake_match(mid, ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), True)

            def match_ids(self, puuid, count=10, queue=None, start=0, since=None):
                return []
        conn = fresh_db()
        conn.execute("INSERT INTO matches (match_id, patch, has_builds) VALUES ('OLD1', '15.23', 0)")
        self.assertEqual(crawl.crawl(Client(), conn, max_requests=10, log=lambda *a: None), 1)
        self.assertTrue(db.has_match(conn, "OLD1"))


class ProfileTests(unittest.TestCase):
    def test_lookup(self):
        conn = fresh_db()
        d = profile.lookup(FakeRiot(), conn, "Test#NA1", champions())
        self.assertEqual(d["riot_id"], "Test#NA1")
        self.assertEqual(d["ranks"][0]["tier"], "Emerald")
        self.assertEqual([g["win"] for g in d["games"]], [True, False])
        self.assertEqual(d["games"][0]["champion"], "Jinx")
        self.assertEqual(d["games"][0]["kda"], "5/2/7")
        self.assertEqual([{k: c[k] for k in ("champion", "games", "wins")} for c in d["champions"]],
                         [{"champion": "Jinx", "games": 2, "wins": 1}])
        g = d["games"][0]
        self.assertEqual(len(g["teams"]), 2)
        self.assertIn("kda_ratio", g)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0], 2)


class PruneTests(unittest.TestCase):
    def test_drops_old_patches_and_keeps_mine(self):
        conn = fresh_db()
        for i, patch in enumerate(["16.21", "16.22", "16.23", "16.23"]):
            conn.execute("INSERT INTO matches (match_id, patch, game_start) VALUES (?,?,?)", (f"m{i}", patch, i))
            conn.execute("INSERT INTO participants (match_id, team_id, champion_id, position, win) VALUES (?,100,1,'TOP',1)", (f"m{i}",))
        conn.execute("INSERT INTO my_games VALUES ('m0', 1, 'TOP', 1, 0)")
        conn.commit()
        r = db.prune(conn, keep_patches=2, max_matches=2)
        left = {x[0] for x in conn.execute("SELECT match_id FROM matches")}
        self.assertIn("m0", left)            # your own game stays even on an old patch
        self.assertNotIn("m1", left)         # over the limit, oldest goes first
        self.assertEqual(left, {"m0", "m2", "m3"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 3)


class SchemaTests(unittest.TestCase):
    """Champ Picker shares a Supabase project with southernjits, so its tables
    must stay in their own schema and the transaction pooler must be refused."""

    def test_refuses_transaction_pooler(self):
        for url in ("postgresql://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres",
                    "postgresql://u:p@db.example.com:5432/postgres?pgbouncer=true"):
            with self.assertRaises(RuntimeError) as cm:
                db.connect(url)
            self.assertIn("Session pooler", str(cm.exception))

    @unittest.skipUnless(PG_URL, "needs TEST_DATABASE_URL")
    def test_tables_go_in_own_schema(self):
        import psycopg
        schema = f"cp{os.getpid()}"
        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        old = os.environ.get("DB_SCHEMA")
        os.environ["DB_SCHEMA"] = schema
        try:
            conn = db.connect(PG_URL)
            conn.execute("INSERT INTO players (puuid) VALUES (?)", ("x",))
            conn.commit()
            conn.close()
            with psycopg.connect(PG_URL) as c:
                tables = {r[0] for r in c.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))}
                self.assertIn("players", tables)
                self.assertIn("matches", tables)
                in_public = c.execute("SELECT count(*) FROM information_schema.tables "
                                      "WHERE table_schema = 'public' AND table_name = 'players'").fetchone()[0]
                self.assertEqual(in_public, 0)
        finally:
            if old is None:
                os.environ.pop("DB_SCHEMA", None)
            else:
                os.environ["DB_SCHEMA"] = old
            with psycopg.connect(PG_URL, autocommit=True) as c:
                c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")

    def test_rejects_bad_schema_name(self):
        old = os.environ.get("DB_SCHEMA")
        os.environ["DB_SCHEMA"] = "x; drop table y"
        try:
            with self.assertRaises(ValueError):
                db.connect("postgresql://u:p@127.0.0.1:1/none?connect_timeout=1")
        finally:
            if old is None:
                os.environ.pop("DB_SCHEMA", None)
            else:
                os.environ["DB_SCHEMA"] = old


if __name__ == "__main__":
    unittest.main()
