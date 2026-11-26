"""Local web app: live best-pick plus counters and tier list.

Serves on 127.0.0.1 only. A background thread watches the League client
for champ select and recomputes picks whenever the draft changes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, engine, lcu
from .config import Config
from .ddragon import Champions
from .stats import Stats

STATIC = Path(__file__).resolve().parent / "static"


class App:
    def __init__(self, cfg: Config, champs: Champions):
        self.cfg = cfg
        self.champs = champs
        self.lock = threading.Lock()
        self.conn = db.connect(cfg.db_path)
        self.stats = Stats(self.conn, cfg.patches_to_use)
        self.live = {"connected": False, "in_champ_select": False}
        self._last_key = None

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
                    "ddragon_version": self.champs.version}

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
                "worst": rows[:10], "best": list(reversed(rows[-10:]))}

    def tierlist(self, role: str, min_games: int = 30) -> list[dict]:
        with self.lock:
            total = self.stats.role_games.get(role, 0)
            out = []
            for (c, pos), (n, w) in self.stats.champ_role.items():
                if pos != role or n < min_games:
                    continue
                out.append({"champion_id": c, "name": self.champs.name(c), "games": n,
                            "win_pct": round(100 * w / n, 1),
                            "adjusted_win_pct": round(100 * engine.shrunk(w, n, 0.5, engine.K_BASE), 1),
                            "pick_pct": round(100 * n / total, 1) if total else 0,
                            "image": self.champs.by_id.get(c, {}).get("image")})
        return sorted(out, key=lambda x: -x["adjusted_win_pct"])

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
                                               "hover": a.hover} for a in cs.allies],
                                   "enemies": [self.champs.name(e) for e in cs.enemies],
                                   "bans": [self.champs.name(b) for b in cs.bans]},
                         "picks": self.recommend(role, cs.allies, cs.enemies, cs.bans),
                         "updated": time.time()}


def _ids(app: App, raw: str) -> list[int]:
    return [c for c in (app.champs.find(x) for x in raw.split(",") if x.strip()) if c]


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            role = q.get("role", app.cfg.role).upper()
            try:
                if url.path in ("/", "/index.html"):
                    body = (STATIC / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/api/status":
                    self._json(app.status())
                elif url.path == "/api/live":
                    self._json(app.live)
                elif url.path == "/api/champions":
                    self._json(app.champs.list())
                elif url.path == "/api/recommend":
                    allies = [engine.Ally(c) for c in _ids(app, q.get("allies", ""))]
                    self._json(app.recommend(role, allies, _ids(app, q.get("enemies", "")),
                                             _ids(app, q.get("bans", "")), top=int(q.get("top", 5))))
                elif url.path == "/api/counters":
                    cid = app.champs.find(q.get("champion", ""))
                    if not cid:
                        return self._json({"error": "unknown champion"}, 400)
                    self._json(app.counters(cid, role))
                elif url.path == "/api/tierlist":
                    self._json(app.tierlist(role))
                else:
                    self._json({"error": "not found"}, 404)
            except (sqlite3.Error, ValueError) as e:
                self._json({"error": str(e)}, 500)

    return Handler


def serve(cfg: Config, champs: Champions, port: int = 8777, watch: bool = True):
    app = App(cfg, champs)
    if watch:
        threading.Thread(target=app.poll_forever, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    print(f"Pick helper running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
