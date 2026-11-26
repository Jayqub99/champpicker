"""The Champ Picker site: live best-pick, champion pages, tier list and player lookup.

Runs two ways with the same code:
- On your PC (`python -m pickhelper serve`): serves on 127.0.0.1, and a background
  thread watches the League client for champ select.
- On Vercel (api/index.py): the companion on your PC sends the draft to
  /api/live, and the site keeps it in the database.

When SITE_PASSWORD is set every page needs it; on Vercel the site refuses to
run without one.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import builds, db, engine, lcu, profile
from .config import ON_VERCEL, Config
from .ddragon import Champions
from .riot import RiotClient, RiotError
from .stats import Stats

STATIC = Path(__file__).resolve().parent / "static"
LIVE_STALE_SECONDS = 20   # companion silent this long = treat as offline
COOKIE = "cp_session"


class App:
    def __init__(self, cfg: Config, champs: Champions, watching: bool = False):
        self.cfg = cfg
        self.champs = champs
        self.watching = watching      # True when this process watches the League client itself
        self.lock = threading.RLock()
        self.conn = db.connect(cfg.db_target)
        self.stats = Stats(self.conn, cfg.patches_to_use)
        self.live = {"connected": False, "in_champ_select": False}
        self._last_key = None
        self._match_count = self._count()
        self.riot = RiotClient(cfg.api_key, cfg.platform, cfg.region) if cfg.api_key else None

    # -- database upkeep ----------------------------------------------------------
    def _count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]

    def ensure_db(self) -> None:
        """Reconnect if a hosted database dropped the connection (it sleeps when idle)."""
        raw = self.conn.raw
        if self.conn.kind == "postgres" and (raw.closed or raw.broken):
            with self.lock:
                self.conn = db.connect(self.cfg.db_target)
                self.stats = Stats(self.conn, self.cfg.patches_to_use)

    def end_request(self) -> None:
        try:
            self.conn.commit()  # ends the read transaction on Postgres
        except Exception:
            pass

    def refresh(self) -> None:
        """Pick up matches a crawl added while the site was running."""
        with self.lock:
            n = self._count()
            if n != self._match_count:
                self._match_count = n
                self.stats = Stats(self.conn, self.cfg.patches_to_use)

    # -- shared -----------------------------------------------------------------
    def pool(self, role: str) -> list[int]:
        fixed = [c for c in (self.champs.find(n) for n in self.cfg.pool) if c]
        return engine.candidate_pool(self.stats, role, fixed if role == self.cfg.role else None)

    def recommend(self, role: str, allies, enemies, bans, top: int = 3) -> list[dict]:
        with self.lock:
            picks = engine.recommend(self.stats, role, allies, enemies, bans, self.pool(role),
                                     names=self.champs.name, top=top)
        return [dict(p.as_dict(), image=self.champs.by_id.get(p.champion_id, {}).get("image"))
                for p in picks]

    def status(self) -> dict:
        with self.lock:
            role = self.cfg.role
            return {"patches": self.stats.patches, "matches": self.stats.match_count,
                    "my_games": self.conn.execute("SELECT COUNT(*) FROM my_games").fetchone()[0],
                    "role": role, "riot_id_set": bool(self.cfg.riot_id),
                    "pool": [self.champs.name(c) for c in self.pool(role)],
                    "ddragon_version": self.champs.version, "hosted": not self.watching,
                    "riot_id": self.cfg.riot_id}

    def counters(self, cid: int, role: str) -> dict:
        with self.lock:
            n, w = self.stats.champ_role.get((cid, role), (0, 0))
            base = engine.shrunk(w, n, 0.5, engine.K_BASE)
            rows = []
            for r in self.stats.lane_opponents(cid, role):
                wr = engine.shrunk(r["w"], r["n"], base, engine.K_PAIR)
                rows.append({"champion_id": r["o"], "name": self.champs.name(r["o"]),
                             "games": r["n"], "win_pct": round(100 * r["w"] / r["n"], 1),
                             "adjusted_win_pct": round(100 * wr, 1)})
        rows.sort(key=lambda x: x["adjusted_win_pct"])
        return {"champion": self.champs.name(cid), "role": role, "games": n,
                "win_pct": round(100 * w / n, 1) if n else None,
                "worst": [r for r in rows if r["adjusted_win_pct"] < 100 * base][:10],
                "best": [r for r in reversed(rows) if r["adjusted_win_pct"] >= 100 * base][:10]}

    def tierlist(self, role: str, min_games: int = 30) -> list[dict]:
        if role == "ALL":
            rows = [r for pos in ROLE_ORDER for r in self.tierlist(pos, min_games)]
            return sorted(rows, key=lambda x: (TIERS.index(x["tier"]), -x["adjusted_win_pct"]))
        with self.lock:
            matches = self.stats.match_count or 1
            bans = self.stats.ban_rates
            matchups = self.stats.role_matchups(role)
            out = []
            for (c, pos), (n, w) in self.stats.champ_role.items():
                if pos != role or n < min_games:
                    continue
                base = engine.shrunk(w, n, 0.5, engine.K_BASE)
                adj = 100 * base
                pick = 100 * n / matches          # share of games with this champ in this role
                ban = 100 * bans.get(c, 0.0)
                hard = sorted(matchups.get(c, []),
                              key=lambda r: engine.shrunk(r["w"], r["n"], base, engine.K_PAIR))[:3]
                out.append({"champion_id": c, "name": self.champs.name(c), "role": role, "games": n,
                            "win_pct": round(100 * w / n, 1), "adjusted_win_pct": round(adj, 1),
                            "pick_pct": round(pick, 1), "ban_pct": round(ban, 1),
                            "tier": _tier(adj, pick + ban),
                            "image": self.champs.by_id.get(c, {}).get("image"),
                            "counters": [{"name": self.champs.name(r["o"]),
                                          "image": self.champs.by_id.get(r["o"], {}).get("image")}
                                         for r in hard]})
        return sorted(out, key=lambda x: (TIERS.index(x["tier"]), -x["adjusted_win_pct"]))

    def champion_page(self, cid: int, role: str | None) -> dict:
        with self.lock:
            roles = self.stats.role_share(cid)
            role = role or (max(roles, key=roles.get) if roles else self.cfg.role)
            page = {"champion": self.champs.by_id.get(cid), "role": role,
                    "roles": {k: round(100 * v, 1) for k, v in sorted(roles.items(), key=lambda kv: -kv[1])},
                    "ban_pct": round(100 * self.stats.ban_rates.get(cid, 0.0), 1),
                    "build": builds.champion_build(self.conn, self.stats.patches, cid, role, self.champs)}
            n, w = self.stats.champ_role.get((cid, role), (0, 0))
            if n:
                adj = 100 * engine.shrunk(w, n, 0.5, engine.K_BASE)
                pick = 100 * n / (self.stats.match_count or 1)
                ranked = sorted(((engine.shrunk(w2, n2, 0.5, engine.K_BASE), c2)
                                 for (c2, p2), (n2, w2) in self.stats.champ_role.items()
                                 if p2 == role and n2 >= 30), reverse=True)
                place = next((i + 1 for i, (_, c2) in enumerate(ranked) if c2 == cid), None)
                page["stats"] = {"games": n, "win_pct": round(100 * w / n, 1), "pick_pct": round(pick, 1),
                                 "ban_pct": page["ban_pct"], "tier": _tier(adj, pick + page["ban_pct"]),
                                 "rank": place, "of": len(ranked)}
            partner = engine.DUO.get(role)
            if partner:
                n, w = self.stats.champ_role.get((cid, role), (0, 0))
                base = engine.shrunk(w, n, 0.5, engine.K_BASE)
                duos = []
                for r in self.stats.partners(cid, role, partner, same_team=True):
                    duos.append({"name": self.champs.name(r["o"]), "games": r["n"],
                                 "win_pct": round(100 * r["w"] / r["n"], 1),
                                 "adjusted_win_pct": round(100 * engine.shrunk(r["w"], r["n"], base, engine.K_PAIR), 1),
                                 "image": self.champs.by_id.get(r["o"], {}).get("image")})
                page["duos"] = sorted(duos, key=lambda x: -x["adjusted_win_pct"])[:8]
                page["duo_role"] = partner
            page["matchups"] = self.counters(cid, role)
        page["abilities"] = self.champs.abilities(cid)
        return page

    def my_stats(self) -> dict:
        """Your own games: champions, lane matchups and a personal pick order."""
        with self.lock:
            rows = self.conn.execute("""
                SELECT g.match_id, g.champion_id, g.position, g.win, g.game_start,
                       p.kills, p.deaths, p.assists, p.cs, m.duration, o.champion_id AS opp
                FROM my_games g
                LEFT JOIN participants p ON p.match_id = g.match_id AND p.champion_id = g.champion_id
                                        AND p.position = g.position
                LEFT JOIN matches m ON m.match_id = g.match_id
                LEFT JOIN participants o ON o.match_id = g.match_id AND o.position = g.position
                                        AND o.team_id <> p.team_id
                ORDER BY g.game_start DESC""").fetchall()
            mastery = {r["champion_id"]: r["points"] for r in
                       self.conn.execute("SELECT champion_id, points FROM my_mastery")}
            role = self.cfg.role

            def agg(group):
                n = len(group)
                w = sum(r["win"] for r in group)
                k = sum(r["kills"] or 0 for r in group)
                d = sum(r["deaths"] or 0 for r in group)
                a = sum(r["assists"] or 0 for r in group)
                mins = sum((r["duration"] or 0) / 60 for r in group if r["cs"] is not None)
                cs = sum(r["cs"] or 0 for r in group)
                return {"games": n, "wins": w, "win_pct": round(100 * w / n, 1) if n else None,
                        "kda": round((k + a) / max(1, d), 2), "kills": round(k / max(1, n), 1),
                        "deaths": round(d / max(1, n), 1), "assists": round(a / max(1, n), 1),
                        "cs_per_min": round(cs / mins, 1) if mins else None}

            by_champ, by_role, by_opp = {}, {}, {}
            for r in rows:
                by_champ.setdefault((r["champion_id"], r["position"]), []).append(r)
                by_role.setdefault(r["position"] or "?", []).append(r)
                if r["opp"] and r["position"] == role:
                    by_opp.setdefault(r["opp"], []).append(r)

            champs = []
            for (cid, pos), group in by_champ.items():
                n, w = self.stats.champ_role.get((cid, pos), (0, 0))
                champs.append({**agg(group), "name": self.champs.name(cid), "role": pos,
                               "image": self._img(cid), "mastery": mastery.get(cid, 0),
                               "global_win_pct": round(100 * w / n, 1) if n >= 30 else None})
            champs.sort(key=lambda c: (-c["games"], -(c["win_pct"] or 0)))
            opps = [{**agg(g), "name": self.champs.name(cid), "image": self._img(cid)}
                    for cid, g in by_opp.items() if len(g) >= 2]
            opps.sort(key=lambda o: (o["win_pct"], -o["games"]))
            recent = [{"win": bool(r["win"]), "name": self.champs.name(r["champion_id"]),
                       "image": self._img(r["champion_id"])} for r in rows[:20]]
        return {"riot_id": self.cfg.riot_id, "role": role, "summary": agg(rows) if rows else None,
                "roles": {k: len(v) for k, v in sorted(by_role.items(), key=lambda kv: -len(kv[1]))},
                "champions": champs[:25], "matchups": opps, "recent": recent,
                "picks": self.recommend(role, [], [], [], top=10)}

    def player(self, riot_id: str) -> dict:
        if not self.riot:
            return {"error": "Add your Riot API key (RIOT_API_KEY) to look up players."}
        conn = db.connect(self.cfg.db_target)  # own connection: lookups take several seconds
        try:
            result = profile.lookup(self.riot, conn, riot_id, self.champs)
            conn.commit()
            return result
        except RiotError as e:
            return {"error": e.hint() or str(e)}
        except ValueError as e:
            return {"error": str(e)}
        finally:
            conn.close()

    # -- live champ select --------------------------------------------------------
    def poll_forever(self, interval: float = 1.5):
        client = None
        while True:
            try:
                if client is None:
                    creds = lcu.find_credentials()
                    client = lcu.LcuClient(creds) if creds else None
                if client is None:
                    self.live = {"connected": False, "in_champ_select": False}
                else:
                    session = client.champ_select()
                    if session is None:
                        self.live = {"connected": True, "in_champ_select": False}
                        self._last_key = None
                    else:
                        self._update(lcu.parse_session(session))
            except OSError:
                client = None  # client closed or restarted; look again next time
                self.live = {"connected": False, "in_champ_select": False}
            except Exception:  # keep the watcher alive no matter what
                traceback.print_exc()
            time.sleep(interval)

    def _update(self, cs: lcu.ChampSelect):
        role = cs.my_position or self.cfg.role
        key = (role, tuple((a.champion_id, a.hover) for a in cs.allies), tuple(cs.enemies), tuple(cs.bans))
        if key != self._last_key:
            self._last_key = key
            self.live = {"connected": True, "in_champ_select": True, "role": role,
                         "draft": {"allies": [{"name": self.champs.name(a.champion_id), "position": a.position,
                                               "hover": a.hover, "image": self._img(a.champion_id)}
                                              for a in cs.allies],
                                   "enemies": [{"name": self.champs.name(e), "image": self._img(e)} for e in cs.enemies],
                                   "bans": [{"name": self.champs.name(b), "image": self._img(b)} for b in cs.bans]},
                         "picks": self.recommend(role, cs.allies, cs.enemies, cs.bans),
                         "updated": time.time()}

    def _img(self, cid: int) -> str | None:
        return self.champs.by_id.get(cid, {}).get("image")

    def save_draft(self, payload: dict) -> None:
        """Store what the companion on your PC sent (hosted mode)."""
        with self.lock:
            self.conn.execute("INSERT INTO live_state (id, payload, updated) VALUES (1, ?, ?) "
                              "ON CONFLICT (id) DO UPDATE SET payload = excluded.payload, updated = excluded.updated",
                              (json.dumps(payload), int(time.time())))
            self.conn.commit()

    def live_state(self) -> dict:
        if self.watching:
            return self.live
        with self.lock:
            row = self.conn.execute("SELECT payload, updated FROM live_state WHERE id = 1").fetchone()
        if not row or time.time() - row["updated"] > LIVE_STALE_SECONDS:
            self._last_key = None
            return {"connected": False, "in_champ_select": False, "companion": False}
        data = json.loads(row["payload"])
        if not data.get("in_champ_select"):
            self._last_key = None
            return {"connected": True, "in_champ_select": False, "companion": True}
        self._update(draft_from_payload(data))
        return self.live


def draft_from_payload(data: dict) -> lcu.ChampSelect:
    def ids(xs):
        return [int(x) for x in xs or [] if str(x).isdigit()]
    allies = [engine.Ally(int(a["champion_id"]), a.get("position"), bool(a.get("hover")))
              for a in data.get("allies") or [] if str(a.get("champion_id", "")).isdigit()]
    return lcu.ChampSelect(data.get("phase", ""), data.get("my_position"), int(data.get("my_champion") or 0),
                           allies, ids(data.get("enemies")), ids(data.get("bans")))


TIERS = ["S+", "S", "A", "B", "C", "D"]
ROLE_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]


def _tier(adjusted_win_pct: float, pick_plus_ban_pct: float) -> str:
    """Tier from win rate vs 50%, nudged up one step for very popular champions."""
    edge = adjusted_win_pct - 50
    i = 0 if edge >= 2.5 else 1 if edge >= 1.5 else 2 if edge >= 0.5 else 3 if edge >= -0.5 else 4 if edge >= -1.5 else 5
    if pick_plus_ban_pct >= 20 and i > 0:
        i -= 1
    return TIERS[i]


def _ids(app: App, raw: str) -> list[int]:
    return [c for c in (app.champs.find(x) for x in raw.split(",") if x.strip()) if c]


def session_token(password: str) -> str:
    return hmac.new(password.encode(), b"champpicker-session", hashlib.sha256).hexdigest()


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Champ Picker</title>
<style>body{margin:0;background:#08081f;color:#fff;font:14px Inter,system-ui,"Segoe UI",sans-serif;display:grid;place-items:center;height:100vh}
form{background:#12122d;border-radius:8px;padding:28px;width:min(340px,90vw)}
h1{font-size:18px;font-weight:800;margin:0 0 16px;display:flex;align-items:center;gap:10px}
h1 span{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;font-size:13px;background:linear-gradient(135deg,#3c7cff,#7a4dff)}
input,button{width:100%;height:40px;padding:0 12px;border-radius:6px;font:inherit;box-sizing:border-box}
input{background:#08081f;color:#fff;border:1px solid #26264a;margin-bottom:10px;outline:none}input:focus{border-color:#3c7cff}
button{background:#3c7cff;color:#fff;border:0;font-weight:600;cursor:pointer}button:hover{background:#2a5fd1}
p{color:#ff5a5f;font-size:13px;margin:0 0 10px}</style></head><body><form method="post" action="/login">
<h1><span>CP</span>Champ Picker</h1>%ERROR%<input type="password" name="password" placeholder="Password" autofocus>
<button>Sign in</button></form></body></html>"""


def make_handler(app_factory):
    """app_factory returns the App (built lazily on Vercel so errors show on the page)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # -- helpers ----------------------------------------------------------
        def _send(self, status: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status=200):
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _route(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = q.pop("__path", None) or url.path   # Vercel rewrite passes the original path
            return path, q

        def _authorized(self, password: str) -> bool:
            if not password:
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], password):
                return True
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE and hmac.compare_digest(value, session_token(password)):
                    return True
            return False

        def _password(self) -> str | None:
            """The site password, or None when the hosted site has none (refuse to serve)."""
            from .config import load
            pw = getattr(self.server, "site_password", None)
            if pw is None:
                pw = load().site_password
            if ON_VERCEL and not pw:
                return None
            return pw

        def _login_page(self, error: str = "", status: int = 200):
            body = LOGIN_PAGE.replace("%ERROR%", f"<p>{error}</p>" if error else "")
            self._send(status, body.encode(), "text/html; charset=utf-8")

        # -- POST: sign in, companion drafts -------------------------------------
        def do_POST(self):
            path, _ = self._route()
            password = self._password()
            if password is None:
                return self._json({"error": "Set SITE_PASSWORD in Vercel first."}, 503)
            length = min(int(self.headers.get("Content-Length") or 0), 100_000)
            body = self.rfile.read(length) if length else b""
            if path == "/login":
                form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
                if password and hmac.compare_digest(form.get("password", ""), password):
                    secure = "; Secure" if ON_VERCEL else ""
                    return self._send(303, b"", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={session_token(password)}; Path=/; HttpOnly; "
                                      f"SameSite=Lax; Max-Age=2592000{secure}"})
                return self._login_page("Wrong password.", 401)
            if not self._authorized(password):
                return self._json({"error": "unauthorized"}, 401)
            if path == "/api/live":
                try:
                    app = app_factory()
                    app.ensure_db()
                    app.save_draft(json.loads(body or b"{}"))
                    return self._json({"ok": True})
                except Exception as e:
                    traceback.print_exc()
                    return self._json({"error": str(e)}, 500)
            self._json({"error": "not found"}, 404)

        # -- GET: pages and data --------------------------------------------------
        def do_GET(self):
            path, q = self._route()
            password = self._password()
            if password is None:
                return self._send(503, b"Set SITE_PASSWORD in your Vercel project settings, then redeploy.",
                                  "text/plain; charset=utf-8")
            if not self._authorized(password):
                if path.startswith("/api/"):
                    return self._json({"error": "unauthorized"}, 401)
                return self._login_page()
            if path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            try:
                app = app_factory()
                app.ensure_db()
                role = q.get("role", app.cfg.role).upper()
                if path == "/api/status":
                    app.refresh()
                    self._json(app.status())
                elif path == "/api/live":
                    self._json(app.live_state())
                elif path == "/api/champions":
                    self._json(app.champs.list())
                elif path == "/api/recommend":
                    allies = [engine.Ally(c) for c in _ids(app, q.get("allies", ""))]
                    self._json(app.recommend(role, allies, _ids(app, q.get("enemies", "")),
                                             _ids(app, q.get("bans", "")), top=int(q.get("top", 5))))
                elif path == "/api/champion":
                    app.refresh()
                    cid = app.champs.find(q.get("champion", ""))
                    if not cid:
                        return self._json({"error": "unknown champion"}, 400)
                    self._json(app.champion_page(cid, q.get("role", "").upper() or None))
                elif path == "/api/me":
                    app.refresh()
                    self._json(app.my_stats())
                elif path == "/api/profile":
                    self._json(app.player(q.get("riot_id", "")))
                elif path == "/api/counters":
                    cid = app.champs.find(q.get("champion", ""))
                    if not cid:
                        return self._json({"error": "unknown champion"}, 400)
                    self._json(app.counters(cid, role))
                elif path == "/api/tierlist":
                    app.refresh()
                    self._json(app.tierlist(role))
                else:
                    self._json({"error": "not found"}, 404)
                app.end_request()
            except Exception as e:
                traceback.print_exc()
                self._json({"error": str(e)}, 500)

    return Handler


def serve(cfg: Config, champs: Champions, port: int = 8777, watch: bool = True):
    app = App(cfg, champs, watching=watch)
    if watch:
        threading.Thread(target=app.poll_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(lambda: app))
    server.site_password = cfg.site_password
    print(f"Champ Picker running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
