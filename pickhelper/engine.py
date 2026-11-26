"""Best-pick scoring.

Each candidate gets a score in log-odds (so effects add up cleanly):

    score = base + sum(matchups) + sum(synergies) + comp + comfort + blind

Small samples are shrunk toward "no effect" so a 40-game fluke can't top the
list. The output is a ranked list with the biggest reasons for each pick; it
never picks for you.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .stats import Stats

K_BASE = 100      # games of prior weight on a champion's role win rate
K_PAIR = 200      # games of prior weight on a matchup or synergy
K_MINE = 5        # games of prior weight on your own win rate

DUO = {"BOTTOM": "UTILITY", "UTILITY": "BOTTOM"}


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def pct(x: float) -> float:
    """A log-odds contribution expressed as win-chance points around 50%."""
    return (sigmoid(x) - 0.5) * 100


def shrunk(wins: float, games: float, prior: float, k: float) -> float:
    return (wins + k * prior) / (games + k)


@dataclass
class Ally:
    champion_id: int
    position: str | None = None   # known from champ select for your own team
    hover: bool = False           # still only a hover, not locked


@dataclass
class Pick:
    champion_id: int
    name: str
    score: float
    win_pct: float
    games_in_role: int
    terms: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"champion_id": self.champion_id, "name": self.name, "score": round(self.score, 4),
                "win_pct": round(self.win_pct, 1), "games_in_role": self.games_in_role,
                "low_data": self.games_in_role < 50, "reasons": self.reasons, "terms": self.terms}


def lane_weight(my_role: str, their_pos: str) -> float:
    if their_pos == my_role:
        return 1.0
    if DUO.get(my_role) == their_pos:
        return 0.8
    return 0.5


def ally_weight(my_role: str, their_pos: str | None) -> float:
    if their_pos and DUO.get(my_role) == their_pos:
        return 0.8
    return 0.6


def candidate_pool(stats: Stats, role: str, fixed: list[int] | None = None) -> list[int]:
    """Your pool: a fixed list, else champions you actually play in this role."""
    if fixed:
        return list(fixed)
    mine = stats.my_role_games(role)
    mastery = stats.my_mastery()
    pool = {c for c, n in mine.items() if n >= 2}
    for c, pts in mastery.items():
        if pts >= 30000 and stats.role_share(c).get(role, 0) >= 0.15:
            pool.add(c)
    if not pool:  # no history yet: fall back to common picks in the role
        pool = set(stats.top_in_role(role, limit=20))
    return sorted(pool)


def recommend(stats: Stats, role: str, allies: list[Ally], enemies: list[int], bans: list[int],
              pool: list[int], names=lambda c: str(c), top: int = 3) -> list[Pick]:
    taken = {a.champion_id for a in allies if not a.hover} | set(enemies) | set(bans)
    cands = [c for c in pool if c not in taken]
    if not cands:
        return []

    ally_ids = [a.champion_id for a in allies]
    vs = stats.pairs(cands, role, enemies, same_team=False)
    syn = stats.pairs(cands, role, ally_ids, same_team=True)
    mine = stats.my_games()
    mastery = stats.my_mastery()
    prof = stats.profiles

    # Which enemy is in your lane? Champ select doesn't say, so use role rates.
    enemy_w = {}
    lane_known = False
    for e in enemies:
        share = stats.role_share(e) or {role: 0.2}
        enemy_w[e] = sum(s * lane_weight(role, p) for p, s in share.items()) or 0.5
        if share.get(role, 0) >= 0.5:
            lane_known = True

    blind_spread = {}
    if not lane_known:
        opponents = stats.top_in_role(role, limit=15)
        lane_vs = stats.pairs(cands, role, opponents, same_team=False, other_pos=role)
        for c in cands:
            base_p = _base_p(stats, c, role)
            deltas = [logit(shrunk(w, n, base_p, K_PAIR)) - logit(base_p)
                      for (cc, _), (n, w) in lane_vs.items() if cc == c]
            if len(deltas) >= 3:
                mean = sum(deltas) / len(deltas)
                blind_spread[c] = math.sqrt(sum((d - mean) ** 2 for d in deltas) / len(deltas))

    picks: list[Pick] = []
    for c in cands:
        n_role, _ = stats.champ_role.get((c, role), (0, 0))
        base_p = _base_p(stats, c, role)
        base = logit(base_p)
        parts: list[tuple[float, str]] = []

        match_terms = {}
        for e in enemies:
            n, w = vs.get((c, e), (0, 0))
            d = (logit(shrunk(w, n, base_p, K_PAIR)) - base) * enemy_w[e]
            match_terms[names(e)] = round(d, 4)
            parts.append((d, f"vs {names(e)}"))

        syn_terms = {}
        for a in allies:
            n, w = syn.get((c, a.champion_id), (0, 0))
            d = (logit(shrunk(w, n, base_p, K_PAIR)) - base) * ally_weight(role, a.position)
            syn_terms[names(a.champion_id)] = round(d, 4)
            parts.append((d, f"with {names(a.champion_id)}"))

        comp, comp_reason = _comp(prof, ally_ids, c)
        if comp_reason:
            parts.append((comp, comp_reason))

        comfort = 0.0
        if mine or mastery:  # no account synced yet: comfort can't tell picks apart
            comfort, comfort_reason = _comfort(mine.get(c, (0, 0)), mastery.get(c, 0))
            parts.append((comfort, comfort_reason))

        blind = -0.5 * blind_spread.get(c, 0.0)
        if blind:
            parts.append((blind, "risky blind pick (matchup-dependent)"))

        total = base + sum(p for p, _ in parts)
        reasons = [f"{pct(v):+.1f}% {label}" for v, label in sorted(parts, key=lambda x: -abs(x[0]))[:3]
                   if abs(pct(v)) >= 0.3]
        picks.append(Pick(c, names(c), total, sigmoid(total) * 100, n_role, {
            "base_win_pct": round(base_p * 100, 1), "matchups": match_terms, "synergies": syn_terms,
            "comp": round(comp, 4), "comfort": round(comfort, 4), "blind": round(blind, 4)}, reasons))

    picks.sort(key=lambda p: -p.score)
    return picks[:top]


def _base_p(stats: Stats, c: int, role: str) -> float:
    n, w = stats.champ_role.get((c, role), (0, 0))
    return shrunk(w, n, 0.5, K_BASE)


def _comp(prof: dict, ally_ids: list[int], c: int) -> tuple[float, str]:
    """Reward filling team gaps: damage mix, frontline, crowd control."""
    known = [a for a in ally_ids if a in prof]
    if len(known) < 2 or c not in prof:
        return 0.0, ""

    def badness(team):
        ap = sum(prof[x]["ap_share"] for x in team) / len(team)
        return max(0.0, 0.3 - ap) + max(0.0, ap - 0.7)

    before, after = badness(known), badness(known + [c])
    score, reason = (before - after) * 1.0, ""
    if score > 0.005:
        ap = sum(prof[x]["ap_share"] for x in known) / len(known)
        reason = "adds magic damage" if ap < 0.3 else "adds physical damage"
    elif score < -0.005:
        reason = "makes damage one-sided"

    if len(known) >= 3:
        if max(prof[x]["tank"] for x in known) < 1.2 and prof[c]["tank"] > 1.0:
            bonus = 0.15 * min(1.0, (prof[c]["tank"] - 1.0) / 0.4)
            score += bonus
            reason = reason or "adds frontline"
        if sum(prof[x]["cc"] for x in known) / len(known) < 0.9 and prof[c]["cc"] > 1.0:
            bonus = 0.1 * min(1.0, (prof[c]["cc"] - 1.0) / 0.5)
            score += bonus
            reason = reason or "adds crowd control"
    return score, reason


def _comfort(games_wins: tuple[int, int], mastery_points: int) -> tuple[float, str]:
    n, w = games_wins
    wr_term = 0.5 * logit(shrunk(w, n, 0.5, K_MINE))
    exp_term = 0.2 * min(1.0, math.log1p(n) / math.log1p(20)) - 0.1
    if n == 0:
        exp_term += 0.05 if mastery_points >= 50000 else -0.1
        return wr_term + exp_term, "not in your recent games"
    return wr_term + exp_term, f"your {n} recent games ({100 * w / n:.0f}% won)"
