"""Aggregate queries over the crawled matches.

Everything is limited to the most recent N patches in the database, so old
balance changes fade out on their own.
"""
from __future__ import annotations

from functools import cached_property

from .db import Database


def _patch_key(p: str):
    try:
        return tuple(int(x) for x in p.split("."))
    except ValueError:
        return (0,)


class Stats:
    def __init__(self, conn: Database, patches_to_use: int = 2):
        self.conn = conn
        rows = conn.execute("SELECT patch, COUNT(*) n FROM matches GROUP BY patch").fetchall()
        ordered = sorted(rows, key=lambda r: _patch_key(r["patch"]), reverse=True)
        self.patches = [r["patch"] for r in ordered[:patches_to_use]]
        self.match_count = sum(r["n"] for r in ordered[:patches_to_use])

    # -- helpers --------------------------------------------------------------
    def _patch_clause(self, alias: str = "m") -> tuple[str, list]:
        if not self.patches:
            return "1=0", []
        return f"{alias}.patch IN ({','.join('?' * len(self.patches))})", list(self.patches)

    @staticmethod
    def _in(ids) -> str:
        return ",".join("?" * len(ids))

    # -- per champion ---------------------------------------------------------
    @cached_property
    def champ_role(self) -> dict[tuple[int, str], tuple[int, int]]:
        clause, args = self._patch_clause()
        q = f"""SELECT p.champion_id c, p.position pos, COUNT(*) n, SUM(p.win) w
                FROM participants p JOIN matches m USING (match_id)
                WHERE {clause} GROUP BY c, pos"""
        return {(r["c"], r["pos"]): (r["n"], r["w"]) for r in self.conn.execute(q, args)}

    @cached_property
    def role_games(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for (_, pos), (n, _) in self.champ_role.items():
            out[pos] = out.get(pos, 0) + n
        return out

    def role_share(self, cid: int) -> dict[str, float]:
        counts = {pos: n for (c, pos), (n, _) in self.champ_role.items() if c == cid}
        total = sum(counts.values())
        return {pos: n / total for pos, n in counts.items()} if total else {}

    @cached_property
    def profiles(self) -> dict[int, dict]:
        """Damage mix, tankiness and crowd control per champion, relative to average."""
        clause, args = self._patch_clause()
        q = f"""SELECT p.champion_id c, COUNT(*) n, AVG(p.phys_dmg) ph, AVG(p.magic_dmg) mg,
                       AVG(p.true_dmg) tr, AVG(p.dmg_taken) tk, AVG(p.cc_time) cc
                FROM participants p JOIN matches m USING (match_id)
                WHERE {clause} GROUP BY c"""
        rows = [{k: (float(r[k]) if r[k] is not None and k != "c" else r[k]) for k in r.keys()}
                for r in self.conn.execute(q, args).fetchall()]
        if not rows:
            return {}
        total = sum(r["n"] for r in rows)
        avg_tk = sum(r["tk"] * r["n"] for r in rows) / total or 1
        avg_cc = sum(r["cc"] * r["n"] for r in rows) / total or 1
        out = {}
        for r in rows:
            dmg = (r["ph"] or 0) + (r["mg"] or 0) + (r["tr"] or 0)
            out[r["c"]] = {"games": r["n"],
                           "ap_share": (r["mg"] or 0) / dmg if dmg else 0.5,
                           "tank": (r["tk"] or 0) / avg_tk,
                           "cc": (r["cc"] or 0) / avg_cc}
        return out

    # -- pairs ----------------------------------------------------------------
    def pairs(self, cands: list[int], pos: str, others: list[int], same_team: bool,
              other_pos: str | None = None) -> dict[tuple[int, int], tuple[int, int]]:
        """Games and wins for each candidate (in `pos`) with or against each other champion."""
        if not cands or not others or not self.patches:
            return {}
        clause, args = self._patch_clause()
        team = "a.team_id = b.team_id AND a.position <> b.position" if same_team else "a.team_id <> b.team_id"
        extra = ""
        params = list(args) + [pos] + list(cands) + list(others)
        if other_pos:
            extra = "AND b.position = ?"
            params.append(other_pos)
        q = f"""SELECT a.champion_id c, b.champion_id o, COUNT(*) n, SUM(a.win) w
                FROM participants a
                JOIN participants b ON a.match_id = b.match_id AND {team}
                JOIN matches m ON m.match_id = a.match_id
                WHERE {clause} AND a.position = ?
                  AND a.champion_id IN ({self._in(cands)}) AND b.champion_id IN ({self._in(others)})
                  {extra}
                GROUP BY c, o"""
        return {(r["c"], r["o"]): (r["n"], r["w"]) for r in self.conn.execute(q, params)}

    def lane_opponents(self, cid: int, pos: str, min_games: int = 10) -> list[dict]:
        """All same-lane matchups for one champion (the counters page)."""
        return self.partners(cid, pos, pos, same_team=False, min_games=min_games)

    def partners(self, cid: int, pos: str, other_pos: str, same_team: bool,
                 min_games: int = 10) -> list[dict]:
        """Results with (duos) or against (counters) each champion in `other_pos`."""
        if not self.patches:
            return []
        clause, args = self._patch_clause()
        team = "=" if same_team else "<>"
        q = f"""SELECT b.champion_id o, COUNT(*) n, SUM(a.win) w
                FROM participants a
                JOIN participants b ON a.match_id = b.match_id AND a.team_id {team} b.team_id
                                    AND b.position = ?
                JOIN matches m ON m.match_id = a.match_id
                WHERE {clause} AND a.position = ? AND a.champion_id = ?
                GROUP BY o HAVING COUNT(*) >= ?"""
        return [dict(r) for r in self.conn.execute(q, [other_pos] + list(args) + [pos, cid, min_games])]

    @cached_property
    def ban_rates(self) -> dict[int, float]:
        """Share of games in which each champion was banned (by either team)."""
        if not self.patches:
            return {}
        clause, args = self._patch_clause()
        q = f"""SELECT b.champion_id c, COUNT(DISTINCT b.match_id) n
                FROM bans b JOIN matches m USING (match_id)
                WHERE {clause} AND m.has_builds = 1 GROUP BY c"""
        games = self.conn.execute(
            f"SELECT COUNT(*) FROM matches m WHERE {clause} AND m.has_builds = 1", args).fetchone()[0]
        return {r["c"]: r["n"] / games for r in self.conn.execute(q, args)} if games else {}

    def top_in_role(self, pos: str, limit: int = 15) -> list[int]:
        ranked = sorted(((n, c) for (c, p), (n, _) in self.champ_role.items() if p == pos), reverse=True)
        return [c for _, c in ranked[:limit]]

    # -- you ------------------------------------------------------------------
    def my_games(self) -> dict[int, tuple[int, int]]:
        q = "SELECT champion_id c, COUNT(*) n, SUM(win) w FROM my_games GROUP BY c"
        return {r["c"]: (r["n"], r["w"]) for r in self.conn.execute(q)}

    def my_role_games(self, pos: str) -> dict[int, int]:
        q = "SELECT champion_id c, COUNT(*) n FROM my_games WHERE position = ? GROUP BY c"
        return {r["c"]: r["n"] for r in self.conn.execute(q, (pos,))}

    def my_mastery(self) -> dict[int, int]:
        return {r["champion_id"]: r["points"] for r in self.conn.execute("SELECT * FROM my_mastery")}
