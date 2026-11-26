"""Read champion select from the League client running on this PC.

The League Client API (LCU) is local-only and not officially supported by
Riot for third-party apps, so endpoints can change between patches. This
module only READS the champ select session: champions, bans and your
assigned position. It never reads other players' names and never picks,
bans or locks anything.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import ssl
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .engine import Ally

LOCKFILE_PATHS = [
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path("/Applications/League of Legends.app/Contents/LoL/lockfile"),
]
POSITIONS = {"top": "TOP", "jungle": "JUNGLE", "middle": "MIDDLE", "bottom": "BOTTOM",
             "utility": "UTILITY"}


@dataclass
class Credentials:
    port: int
    password: str


def parse_lockfile(text: str) -> Credentials:
    # Format: LeagueClient:<pid>:<port>:<password>:<protocol>
    parts = text.strip().split(":")
    return Credentials(int(parts[2]), parts[3])


def parse_cmdline(cmdline: str) -> Credentials | None:
    port = re.search(r"--app-port=(\d+)", cmdline)
    token = re.search(r"--remoting-auth-token=([\w-]+)", cmdline)
    if port and token:
        return Credentials(int(port.group(1)), token.group(1))
    return None


def find_credentials() -> Credentials | None:
    env = os.environ.get("LCU_LOCKFILE")
    for path in ([Path(env)] if env else []) + LOCKFILE_PATHS:
        if path.exists():
            try:
                return parse_lockfile(path.read_text())
            except (OSError, IndexError, ValueError):
                pass
    try:
        if platform.system() == "Windows":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" "
                 "| Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=10).stdout
        else:
            out = subprocess.run(["ps", "-A", "-o", "args"], capture_output=True, text=True,
                                 timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_cmdline(out)


class LcuClient:
    def __init__(self, creds: Credentials):
        self.base = f"https://127.0.0.1:{creds.port}"
        token = base64.b64encode(f"riot:{creds.password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        # The client serves a self-signed Riot certificate on localhost only.
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str):
        req = urllib.request.Request(self.base + path, headers=self.headers)
        with urllib.request.urlopen(req, context=self.ctx, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))

    def champ_select(self) -> dict | None:
        try:
            return self.get("/lol-champ-select/v1/session")
        except urllib.error.HTTPError as e:
            if e.code == 404:  # not in champ select
                return None
            raise


@dataclass
class ChampSelect:
    phase: str
    my_position: str | None
    my_champion: int
    allies: list[Ally]
    enemies: list[int]
    bans: list[int]

    def as_dict(self) -> dict:
        return {"phase": self.phase, "my_position": self.my_position, "my_champion": self.my_champion,
                "allies": [a.__dict__ for a in self.allies], "enemies": self.enemies, "bans": self.bans}


def parse_session(s: dict) -> ChampSelect:
    me = s.get("localPlayerCellId")
    allies, my_pos, my_champ = [], None, 0
    for p in s.get("myTeam", []):
        champ = p.get("championId") or 0
        intent = p.get("championPickIntent") or 0
        pos = POSITIONS.get((p.get("assignedPosition") or "").lower())
        if p.get("cellId") == me:
            my_pos, my_champ = pos, champ or intent
            continue
        if champ:
            allies.append(Ally(champ, pos, hover=False))
        elif intent:
            allies.append(Ally(intent, pos, hover=True))
    enemies = [p["championId"] for p in s.get("theirTeam", []) if p.get("championId")]

    bans = set()
    b = s.get("bans") or {}
    bans.update(x for x in (b.get("myTeamBans") or []) + (b.get("theirTeamBans") or []) if x)
    for group in s.get("actions") or []:
        for a in group:
            if a.get("type") == "ban" and a.get("completed") and a.get("championId"):
                bans.add(a["championId"])
    # A champion the enemy locked counts as an enemy even if the team list lags.
    for group in s.get("actions") or []:
        for a in group:
            if (a.get("type") == "pick" and a.get("completed") and not a.get("isAllyAction", True)
                    and a.get("championId") and a["championId"] not in enemies):
                enemies.append(a["championId"])
    phase = (s.get("timer") or {}).get("phase", "")
    return ChampSelect(phase, my_pos, my_champ, allies, enemies, sorted(bans))
