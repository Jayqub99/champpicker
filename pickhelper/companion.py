"""Champ-select companion for the hosted site.

Run it on your PC while you play: it reads the draft from the League client
(read-only, never your teammates' names) and sends it to your site, which
then shows live picks. Stop it with Ctrl+C when you're done.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import lcu

HEARTBEAT_SECONDS = 8


def payload_from(cs: lcu.ChampSelect | None) -> dict:
    if cs is None:
        return {"in_champ_select": False}
    return {"in_champ_select": True, "phase": cs.phase, "my_position": cs.my_position,
            "my_champion": cs.my_champion,
            "allies": [{"champion_id": a.champion_id, "position": a.position, "hover": a.hover}
                       for a in cs.allies],
            "enemies": cs.enemies, "bans": cs.bans}


def send(site_url: str, password: str, payload: dict, opener=urllib.request.urlopen) -> None:
    req = urllib.request.Request(site_url.rstrip("/") + "/api/live", data=json.dumps(payload).encode(),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "Authorization": f"Bearer {password}",
                                                         "User-Agent": "champpicker-companion"})
    with opener(req, timeout=10) as r:
        r.read()


def run(site_url: str, password: str, interval: float = 1.5, log=print) -> None:
    if not site_url.startswith("https://") and "127.0.0.1" not in site_url and "localhost" not in site_url:
        raise SystemExit("site_url must start with https://")
    log(f"Sending champ select to {site_url}. Leave this open while you play; Ctrl+C to stop.")
    client, last, last_sent = None, None, 0.0
    while True:
        try:
            if client is None:
                creds = lcu.find_credentials()
                client = lcu.LcuClient(creds) if creds else None
            session = client.champ_select() if client else None
            payload = payload_from(lcu.parse_session(session) if session else None)
            if not client:
                payload["client"] = False
            if payload != last or time.time() - last_sent > HEARTBEAT_SECONDS:
                send(site_url, password, payload)
                if payload != last:
                    log("  in champ select" if payload["in_champ_select"] else "  waiting for champ select")
                last, last_sent = payload, time.time()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SystemExit("The site rejected the password. Check site_password in config.json.")
            log(f"  site error {e.code}, retrying")
        except OSError:
            client = None  # League closed, or the site/network blipped
        time.sleep(interval)
