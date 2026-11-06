"""Shared helpers: HTTP session, time zones, logging, source status tracking."""
from __future__ import annotations

import datetime as dt
import logging
import os
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("dashboard")

DUBLIN = ZoneInfo("Europe/Dublin")
UTC = dt.timezone.utc
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def session() -> requests.Session:
    s = requests.Session()
    # Never honour long Retry-After headers (Forex Factory sends 300s when rate
    # limited): a slow source should fail fast and be labelled, not stall the build.
    retry = Retry(total=2, connect=1, read=1, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    return s


HTTP = session()
_DOWN_HOSTS: dict[str, str] = {}


def get(url: str, timeout: float = 30, **kw) -> requests.Response:
    """GET with a short connect timeout. A host that could not be reached is
    skipped for the rest of the run, so a network outage costs seconds per
    host instead of stalling the build on every request."""
    host = urlsplit(url).netloc
    if host in _DOWN_HOSTS:
        raise requests.ConnectionError(f"{host} unreachable earlier this run ({_DOWN_HOSTS[host]})")
    try:
        r = HTTP.get(url, timeout=(8, timeout), **kw)
    except (requests.ConnectionError, requests.Timeout) as e:
        _DOWN_HOSTS[host] = type(e).__name__
        raise
    r.raise_for_status()
    return r


def now_utc() -> dt.datetime:
    # DASHBOARD_NOW lets you rebuild "as of" another moment when testing.
    override = os.environ.get("DASHBOARD_NOW")
    if override:
        return dt.datetime.fromisoformat(override).astimezone(UTC)
    return dt.datetime.now(UTC)


def now_dublin() -> dt.datetime:
    return now_utc().astimezone(DUBLIN)


def parse_local_time(spec: str) -> tuple[dt.time, ZoneInfo]:
    """'16:00 America/New_York' -> (time(16,0), ZoneInfo)."""
    t, tz = spec.split()
    hh, mm = t.split(":")
    return dt.time(int(hh), int(mm)), ZoneInfo(tz)


def at_local(day: dt.date, spec: str) -> dt.datetime:
    """Datetime (aware) for `day` at the local time described by spec."""
    t, tz = parse_local_time(spec)
    return dt.datetime.combine(day, t, tzinfo=tz)


def last_completed_session(close_spec: str, now: dt.datetime | None = None) -> dt.date:
    """Latest weekday whose session close (per close_spec) has passed."""
    now = now or now_utc()
    t, tz = parse_local_time(close_spec)
    local = now.astimezone(tz)
    day = local.date()
    if local.time() < t:
        day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def fmt_dublin(ts: dt.datetime, fmt: str = "%a %d %b %H:%M") -> str:
    return ts.astimezone(DUBLIN).strftime(fmt)


class Status:
    """Records per-source success/failure so the page can show what broke."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def ok(self, key: str, source: str, detail: str = "") -> None:
        self.items[key] = {"ok": True, "source": source, "detail": detail,
                           "at": now_utc().isoformat()}

    def fail(self, key: str, source: str, error: Exception | str) -> None:
        msg = str(error)[:300]
        log.warning("source failed: %s via %s: %s", key, source, msg)
        prev = self.items.get(key)
        if prev and prev.get("ok"):
            return  # an earlier source in the chain already succeeded
        self.items[key] = {"ok": False, "source": source, "detail": msg,
                           "at": now_utc().isoformat()}

    def to_dict(self) -> dict:
        return dict(self.items)
