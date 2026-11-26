import io
import json
import random
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pickhelper import db, engine, lcu  # noqa: E402
from pickhelper.config import Config  # noqa: E402
from pickhelper.ddragon import Champions  # noqa: E402
from pickhelper.riot import RateLimiter, RiotClient, RiotError  # noqa: E402
from pickhelper.stats import Stats  # noqa: E402

# Tiny fake champion list: id -> (name, usual role)
CHAMPS = {1: ("Jinx", "BOTTOM"), 2: ("Caitlyn", "BOTTOM"), 3: ("Ezreal", "BOTTOM"),
          4: ("Draven", "BOTTOM"), 5: ("Thresh", "UTILITY"), 6: ("Lux", "UTILITY"),
          7: ("Darius", "TOP"), 8: ("Ahri", "MIDDLE"), 9: ("LeeSin", "JUNGLE"),
          10: ("Garen", "TOP"), 11: ("Zed", "MIDDLE"), 12: ("Vi", "JUNGLE"),
          13: ("Nautilus", "UTILITY")}
POS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


def champions():
    data = {"data": {n: {"key": str(i), "name": n, "id": n, "tags": [], "image": {"full": n + ".png"}}
                     for i, (n, _) in CHAMPS.items()}}
    return Champions("1.0.1", data)


def fake_match(mid, picks, blue_wins, patch="15.23"):
    parts = []
    for team, champs in ((100, picks[0]), (200, picks[1])):
        for pos, c in zip(POS, champs):
            magic = 20000 if CHAMPS[c][0] in ("Lux", "Ahri") else 1000
            parts.append({"teamId": team, "championId": c, "teamPosition": pos,
                          "win": (team == 100) == blue_wins, "puuid": f"p{team}{pos}",
                          "physicalDamageDealtToChampions": 15000, "magicDamageDealtToChampions": magic,
                          "trueDamageDealtToChampions": 500, "totalDamageTaken": 20000,
                          "timeCCingOthers": 10})
    return {"metadata": {"matchId": mid},
            "info": {"gameDuration": 1800, "gameVersion": f"{patch}.1.1", "queueId": 420,
                     "gameStartTimestamp": 0, "participants": parts}}


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
        cls.conn = db.connect(":memory:")
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
        conn = db.connect(":memory:")
        build_db(conn, n=600)
        for i in range(30):  # 30 Caitlyn games, 24 won
            conn.execute("INSERT INTO my_games VALUES (?,?,?,?,?)", (f"me{i}", 2, "BOTTOM", int(i < 24), 0))
        stats = Stats(conn, 2)
        no_enemy = engine.recommend(stats, "BOTTOM", [], [], [], [2, 3], names=self.names)
        self.assertEqual(no_enemy[0].name, "Caitlyn")

    def test_pool_from_history(self):
        conn = db.connect(":memory:")
        build_db(conn, n=300)
        conn.execute("INSERT INTO my_games VALUES ('a',3,'BOTTOM',1,0)")
        conn.execute("INSERT INTO my_games VALUES ('b',3,'BOTTOM',0,0)")
        conn.execute("INSERT INTO my_games VALUES ('c',2,'BOTTOM',1,0)")
        self.assertEqual(engine.candidate_pool(Stats(conn, 2), "BOTTOM"), [3])

    def test_empty_database_is_safe(self):
        stats = Stats(db.connect(":memory:"), 2)
        self.assertEqual(stats.patches, [])
        picks = engine.recommend(stats, "BOTTOM", [], [4], [], [1, 2], names=self.names)
        self.assertEqual(len(picks), 2)
        self.assertAlmostEqual(picks[0].win_pct, picks[1].win_pct, delta=0.01)

    def test_only_recent_patches_count(self):
        conn = db.connect(":memory:")
        build_db(conn, n=200)
        db.store_match(conn, fake_match("old", ([7, 9, 8, 1, 5], [10, 12, 11, 4, 13]), True, patch="14.1"))
        self.assertEqual(Stats(conn, 1).patches, ["15.23"])
        self.assertEqual(Stats(conn, 5).patches, ["15.23", "14.1"])


class StoreTests(unittest.TestCase):
    def test_remake_and_missing_roles_skipped(self):
        conn = db.connect(":memory:")
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
        tmp = tempfile.mkdtemp()
        cfg = Config(role="BOTTOM")
        cfg_path = Path(tmp) / "x.sqlite3"
        type(cfg).db_path = property(lambda self: cfg_path)
        conn = db.connect(cfg_path)
        build_db(conn, n=800)
        conn.close()
        app = server.App(cfg, champions())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        get = lambda p: json.loads(urllib.request.urlopen(base + p).read())
        try:
            self.assertEqual(get("/api/status")["matches"], 800)
            recs = get("/api/recommend?enemies=Draven&allies=Thresh")
            self.assertEqual(recs[0]["name"], "Jinx")
            counters = get("/api/counters?champion=Draven&role=BOTTOM")
            self.assertEqual(counters["worst"][0]["name"], "Jinx")
            self.assertTrue(get("/api/tierlist?role=BOTTOM"))
            self.assertEqual(get("/api/live"), {"connected": False, "in_champ_select": False})
            html = urllib.request.urlopen(base + "/").read().decode()
            self.assertIn("Pick Helper", html)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
