"""Finance and economics headlines from free RSS feeds: last 24h, deduplicated, tagged."""
from __future__ import annotations

import calendar
import datetime as dt
import html
import re
from difflib import SequenceMatcher

import feedparser

from .util import UTC, Status, get, now_utc

CATEGORY_WEIGHT = {"Central banks": 3, "Macro data": 2, "Geopolitics": 1.5, "Commodities": 1, "Companies": 0.5}
# Stories about the market as a whole, or the handful of companies big enough to move it
MARKET_WIDE = re.compile(r"\b(markets?|stocks|shares|equities|wall street|s&p|nasdaq|dow|ftse|stoxx|nikkei|bonds?|yields?|"
                         r"treasur\w*|gilts?|bunds?|dollar|sterling|euro|yen|currenc\w*|oil prices?|sell-?off|rally|"
                         r"recession|tariffs?|inflation|rates?)\b", re.I)
MEGACAP = re.compile(r"\b(nvidia|apple|microsoft|alphabet|google|amazon|meta|tesla|broadcom|tsmc|jpmorgan|berkshire|opec)\b", re.I)
# Personal-finance and stock-tip pieces: rarely "what's going on in markets"
LOW_VALUE = re.compile(r"(here'?s (how|why|what)|how to|should you|ways to|\bbest\b|\btop \d|could (march|soar|rise|double)|"
                       r"retire|retirement|my |\bi'?m\b|\byour (money|portfolio|savings)|buy (now|before)|millionaire|dividend stock)", re.I)
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

    # Rank by how much a story matters to markets, then keep the top few with
    # at most `max_shown_per_source` from any one outlet.
    def score(it):
        t = it["title"]
        sc = CATEGORY_WEIGHT.get(it["category"], 0)
        sc += 2 * bool(MARKET_WIDE.search(t)) + 3 * bool(MEGACAP.search(t))
        sc += min(len(set(it.get("also", [])) - {it["source"]}), 3)
        sc -= 3 * bool(LOW_VALUE.search(t)) + (t.rstrip().endswith("?"))
        age_h = (now_utc() - dt.datetime.fromisoformat(it["time"])).total_seconds() / 3600
        return sc + max(0.0, 1 - age_h / ncfg["hours"])

    # A cap per topic too, so one busy theme can't push out every other story.
    show, cap = ncfg.get("show", 6), ncfg.get("max_shown_per_source", 2)
    cat_cap = ncfg.get("max_shown_per_category", 3)
    picked, per_source, per_cat, companies = [], {}, {}, set()
    for it in sorted(unique, key=score, reverse=True):
        if per_source.get(it["source"], 0) >= cap or per_cat.get(it["category"], 0) >= cat_cap:
            continue
        # one story per big company: different angles on the same news read as repeats
        names = {m.lower() for m in MEGACAP.findall(it["title"])}
        if names & companies:
            continue
        companies |= names
        picked.append(it)
        per_source[it["source"]] = per_source.get(it["source"], 0) + 1
        per_cat[it["category"]] = per_cat.get(it["category"], 0) + 1
        if len(picked) >= show:
            break
    for it in picked:
        it.pop("_tokens", None)
        it["category"] = it["category"] or "Markets"
        it["also"] = sorted(set(it.get("also", [])) - {it["source"]})
    return picked
