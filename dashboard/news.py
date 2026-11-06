"""Finance and economics headlines from free RSS feeds: last 24h, deduplicated, tagged."""
from __future__ import annotations

import calendar
import datetime as dt
import html
import re
from difflib import SequenceMatcher

import feedparser

from .util import UTC, Status, get, now_utc

STOP = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "as", "at", "is", "after",
        "with", "by", "from", "its", "it", "be", "are", "us", "says", "say"}


def _norm(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOP}


def _similar(a: dict, b: dict) -> bool:
    ta, tb = a["_tokens"], b["_tokens"]
    if ta and tb:
        overlap = len(ta & tb)
        if overlap / len(ta | tb) >= 0.5 or (overlap >= 5 and overlap / len(ta | tb) >= 0.4):
            return True
    return SequenceMatcher(None, a["title"].lower(), b["title"].lower()).ratio() >= 0.75


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def fetch_news(cfg: dict, status: Status) -> list[dict]:
    ncfg = cfg["news"]
    cutoff = now_utc() - dt.timedelta(hours=ncfg["hours"])
    cats = [(name, re.compile(rx, re.I)) for name, rx in ncfg["categories"].items()]
    items = []
    for feed in ncfg["feeds"]:
        try:
            parsed = feedparser.parse(get(feed["url"], timeout=25).content)
            n = 0
            for e in parsed.entries:
                tm = e.get("published_parsed") or e.get("updated_parsed")
                if not tm:
                    continue
                ts = dt.datetime.fromtimestamp(calendar.timegm(tm), UTC)
                if ts < cutoff or ts > now_utc() + dt.timedelta(hours=1):
                    continue
                title = _clean(e.get("title", ""))
                if not title:
                    continue
                summary = _clean(e.get("summary", ""))[:400]
                cat = (feed.get("category")
                       or next((c for c, rx in cats if rx.search(title)), None)
                       or next((c for c, rx in cats if rx.search(summary)), None))
                items.append({"title": title, "link": e.get("link"), "source": feed["name"],
                              "time": ts.isoformat(), "category": cat, "_tokens": _norm(title)})
                n += 1
            status.ok(f"news_{feed['name']}", feed["name"], f"{n} items in last {ncfg['hours']}h")
        except Exception as e:
            status.fail(f"news_{feed['name']}", feed["name"], e)

    items.sort(key=lambda x: x["time"], reverse=True)
    unique: list[dict] = []
    for it in items:
        dup = next((u for u in unique if _similar(u, it)), None)
        if dup:
            dup.setdefault("also", []).append(it["source"])
        else:
            unique.append(it)

    # Prefer categorised stories and stories carried by several outlets; cap per source.
    def score(it):
        return (it["category"] is not None, len(it.get("also", [])), it["time"])

    picked, per_source = [], {}
    for it in sorted(unique, key=score, reverse=True):
        if it["category"] is None and sum(1 for p in picked if p["category"]) >= ncfg["min_items"]:
            continue
        if per_source.get(it["source"], 0) >= ncfg["max_per_source"]:
            continue
        picked.append(it)
        per_source[it["source"]] = per_source.get(it["source"], 0) + 1
        if len(picked) >= ncfg["max_items"]:
            break
    for it in picked:
        it.pop("_tokens", None)
        it["category"] = it["category"] or "Markets"
        it["also"] = sorted(set(it.get("also", [])) - {it["source"]})
    picked.sort(key=lambda x: x["time"], reverse=True)
    return picked
