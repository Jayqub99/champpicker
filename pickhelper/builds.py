"""u.gg-style build pages: runes, summoner spells, items and skill order.

Picks follow u.gg's published rule: the most common option whose win rate
beats the champion's own win rate, among options with a real sample.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations

from .ddragon import Champions
from .db import Database


def _split(text: str | None) -> list[int]:
    return [int(x) for x in (text or "").split(",") if x.strip().lstrip("-").isdigit()]


def _best(counts: dict, base_wr: float, total: int):
    """Most frequent option that beats the baseline win rate; else simply the most frequent."""
    if not counts:
        return None
    floor = max(10, int(total * 0.05))
    ranked = sorted(counts.items(), key=lambda kv: -kv[1][0])
    for key, (n, w) in ranked:
        if n >= floor and w / n >= base_wr:
            return key, n, w
    key, (n, w) = ranked[0]
    return key, n, w


def _add(counter: dict, key, win: int):
    n, w = counter.get(key, (0, 0))
    counter[key] = (n + 1, w + win)


def _stat(n: int, w: int, total: int) -> dict:
    return {"games": n, "win_pct": round(100 * w / n, 1) if n else None,
            "pick_pct": round(100 * n / total, 1) if total else None}


def skill_priority(orders: list[str]) -> str:
    """Q > W > E style max order from the first 13 levels of each game."""
    votes = Counter()
    for order in orders:
        counts = Counter(c for c in order[:13] if c in "QWE")
        if len(counts) == 3:
            votes[">".join(sorted("QWE", key=lambda k: -counts[k]))] += 1
    return votes.most_common(1)[0][0].replace(">", " > ") if votes else ""


def champion_build(conn: Database, patches: list[str], cid: int, pos: str,
                   game: Champions) -> dict:
    if not patches:
        return {"games": 0}
    marks = ",".join("?" * len(patches))
    rows = conn.execute(f"""
        SELECT p.win, p.items, p.spells, p.runes, p.styles, p.shards,
               p.skill_order, p.start_items, p.item_order
        FROM participants p JOIN matches m USING (match_id)
        WHERE m.patch IN ({marks}) AND m.has_builds = 1
          AND p.champion_id = ? AND p.position = ?""", [*patches, cid, pos]).fetchall()
    total = len(rows)
    if not total:
        return {"games": 0}
    wins = sum(r["win"] for r in rows)
    base = wins / total

    pages, spells, cores, boots, starts, orders = {}, {}, {}, {}, {}, {}
    item_seen, skill_orders = {}, []
    later = [{}, {}, {}]  # 4th, 5th and 6th legendary bought (needs timelines)
    for r in rows:
        win = r["win"]
        if r["runes"]:
            _add(pages, (r["runes"], r["styles"], r["shards"]), win)
        if r["spells"]:
            _add(spells, r["spells"], win)
        final = _split(r["items"])
        legend = sorted({i for i in final if game.item(i)["legendary"]})
        for i in legend:
            _add(item_seen, i, win)
        for combo in combinations(legend, 3):
            _add(cores, combo, win)
        for i in final:
            if game.item(i)["boots"]:
                _add(boots, i, win)
                break
        if r["start_items"]:
            _add(starts, tuple(sorted(_split(r["start_items"]))), win)
        order = _split(r["item_order"])
        order = [i for i in order if game.item(i)["legendary"]]
        if len(order) >= 3:
            _add(orders, tuple(order[:3]), win)
        for slot, i in enumerate(order[3:6]):
            _add(later[slot], i, win)
        if r["skill_order"]:
            skill_orders.append(r["skill_order"])

    out = {"games": total, "win_pct": round(100 * base, 1)}

    page = _best(pages, base, total)
    if page:
        (runes, styles, shards), n, w = page
        out["runes"] = {**_stat(n, w, total),
                        "styles": [game.rune(s) for s in _split(styles)],
                        "runes": [game.rune(x) for x in _split(runes)],
                        "shards": [game.rune(x) for x in _split(shards)],
                        "trees": [game.tree(st) for st in _split(styles)]}

    pick = _best(spells, base, total)
    if pick:
        key, n, w = pick
        out["spells"] = {**_stat(n, w, total), "spells": [game.spell(s) for s in _split(key)]}

    core = _best(cores, base, total)
    if core:
        key, n, w = core
        out["core"] = {**_stat(n, w, total), "items": [game.item(i) for i in key],
                       "ordered": False}
    ordered = _best(orders, base, total)
    if ordered and ordered[1] >= 10:
        key, n, w = ordered
        out["core"] = {**_stat(n, w, total), "items": [game.item(i) for i in key], "ordered": True}

    boot = _best(boots, base, total)
    if boot:
        key, n, w = boot
        out["boots"] = {**_stat(n, w, total), "item": game.item(key)}

    start = _best(starts, base, total)
    if start:
        key, n, w = start
        out["start"] = {**_stat(n, w, total), "items": [game.item(i) for i in key]}

    popular = sorted(item_seen.items(), key=lambda kv: -kv[1][0])[:8]
    out["items"] = [{**_stat(n, w, total), "item": game.item(i)} for i, (n, w) in popular]

    for slot, name in enumerate(("fourth", "fifth", "sixth")):
        seen = later[slot]
        slot_total = sum(n for n, _ in seen.values())
        top = sorted(seen.items(), key=lambda kv: -kv[1][0])[:3]
        if top and top[0][1][0] >= 3:
            out[name] = [{**_stat(n, w, slot_total), "item": game.item(i)} for i, (n, w) in top]

    if skill_orders:
        first = Counter(o[:15] for o in skill_orders if len(o) >= 15).most_common(1)
        out["skills"] = {"priority": skill_priority(skill_orders),
                         "order": first[0][0] if first else "",
                         "games": len(skill_orders)}
    return out
