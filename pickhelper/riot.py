"""Minimal Riot API client with the personal-key rate limits built in.

Personal keys allow 20 requests per second and 100 per 2 minutes. Limits
apply per routing host, so each host gets its own limiter. A 429 answer is
honored by sleeping for Retry-After and trying again.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Callable

PERSONAL_LIMITS = ((20, 1.0), (100, 120.0))

# Riot sits behind Cloudflare, which rejects Python's default "Python-urllib"
# User-Agent with "error code: 1010". Always send our own.
USER_AGENT = "champpicker/0.1 (personal League draft helper)"


class RiotError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Riot API {status}: {message}")
        self.status = status
        self.body = message

    def hint(self) -> str:
        if "1010" in self.body:
            return "Riot's firewall rejected the request signature. Update champpicker (git pull)."
        if self.status in (401, 403):
            return ("Riot rejected the API key. Development keys expire every 24 hours: "
                    "get a fresh one from the Riot Developer Portal and update config.json.")
        if self.status == 404:
            return "Not found. Check riot_id in config.json (Name#TAG) and platform/region."
        return ""


class RateLimiter:
    """Sliding-window limiter over several (count, seconds) windows."""

    def __init__(self, limits=PERSONAL_LIMITS, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.limits = limits
        self.clock = clock
        self.sleep = sleep
        self.windows = [deque() for _ in limits]

    def wait_time(self) -> float:
        now = self.clock()
        wait = 0.0
        for (count, seconds), q in zip(self.limits, self.windows):
            while q and now - q[0] >= seconds:
                q.popleft()
            if len(q) >= count:
                wait = max(wait, seconds - (now - q[0]) + 0.01)
        return wait

    def acquire(self) -> None:
        while True:
            wait = self.wait_time()
            if wait <= 0:
                break
            self.sleep(wait)
        now = self.clock()
        for q in self.windows:
            q.append(now)


class RiotClient:
    def __init__(self, api_key: str, platform: str = "na1", region: str = "americas",
                 limits=PERSONAL_LIMITS, opener: Callable | None = None):
        if not api_key:
            raise ValueError("No Riot API key. Set RIOT_API_KEY or api_key in config.json.")
        self.api_key = api_key
        self.platform = platform
        self.region = region
        self.limits = limits
        self._limiters: dict[str, RateLimiter] = {}
        self._open = opener or urllib.request.urlopen
        self.requests_made = 0

    # -- plumbing -----------------------------------------------------------
    def _host(self, regional: bool) -> str:
        return f"{self.region if regional else self.platform}.api.riotgames.com"

    def get(self, path: str, regional: bool = False, params: dict | None = None,
            retries: int = 3) -> Any:
        host = self._host(regional)
        limiter = self._limiters.setdefault(host, RateLimiter(self.limits))
        url = f"https://{host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(retries + 1):
            limiter.acquire()
            req = urllib.request.Request(url, headers={"X-Riot-Token": self.api_key,
                                                         "User-Agent": USER_AGENT})
            self.requests_made += 1
            try:
                with self._open(req, timeout=20) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries:
                    time.sleep(float(e.headers.get("Retry-After", "5")))
                    continue
                if e.code >= 500 and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                body = e.read().decode("utf-8", "replace")[:200]
                raise RiotError(e.code, body) from None
        raise RiotError(429, "rate limited")

    # -- endpoints ----------------------------------------------------------
    def account_by_riot_id(self, riot_id: str) -> dict:
        name, _, tag = riot_id.partition("#")
        if not name or not tag:
            raise ValueError("Riot ID must look like Name#TAG")
        return self.get(f"/riot/account/v1/accounts/by-riot-id/"
                        f"{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}", regional=True)

    def masteries(self, puuid: str) -> list[dict]:
        return self.get(f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}")

    def league_entries(self, tier: str, division: str, page: int = 1) -> list[dict]:
        return self.get(f"/lol/league/v4/entries/RANKED_SOLO_5x5/{tier}/{division}",
                        params={"page": page})

    def apex_league(self, tier: str) -> list[dict]:
        name = {"CHALLENGER": "challengerleagues", "GRANDMASTER": "grandmasterleagues",
                "MASTER": "masterleagues"}[tier]
        return self.get(f"/lol/league/v4/{name}/by-queue/RANKED_SOLO_5x5").get("entries", [])

    def summoner_by_puuid(self, puuid: str) -> dict:
        return self.get(f"/lol/summoner/v4/summoners/by-puuid/{puuid}")

    def summoner(self, summoner_id: str) -> dict:
        return self.get(f"/lol/summoner/v4/summoners/{summoner_id}")

    def match_ids(self, puuid: str, count: int = 20, start: int = 0, queue: int | None = 420) -> list[str]:
        params = {"start": start, "count": count}
        if queue:
            params["queue"] = queue
        return self.get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids", regional=True, params=params)

    def match(self, match_id: str) -> dict:
        return self.get(f"/lol/match/v5/matches/{match_id}", regional=True)

    def timeline(self, match_id: str) -> dict:
        return self.get(f"/lol/match/v5/matches/{match_id}/timeline", regional=True)

    def ranks(self, puuid: str) -> list[dict]:
        return self.get(f"/lol/league/v4/entries/by-puuid/{puuid}")
