"""Economic calendar.

Sources:
  * TradingView economic-calendar API (primary): time, actual, consensus, previous.
  * Forex Factory weekly JSON: cross-checks release times, fills missing consensus.
  * CSO Ireland release calendar + press statements: Irish HICP flash (not on TradingView).
  * FRED release dates (if FRED_API_KEY is set): cross-checks US release dates.
  * central_banks.yaml: the only source of central-bank meeting dates.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import os
import re
from zoneinfo import ZoneInfo

from .util import DUBLIN, UTC, Status, at_local, get, now_utc

log = logging.getLogger("dashboard")

FF_CCY = {"USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP", "CNY": "CN", "AUD": "AU",
          "CAD": "CA", "CHF": "CH", "NZD": "NZ"}
COUNTRY_NAMES = {"US": "US", "EU": "Euro area", "GB": "UK", "IE": "Ireland", "JP": "Japan",
                 "CN": "China", "AU": "Australia", "CA": "Canada", "CH": "Switzerland",
                 "NZ": "New Zealand"}
COUNTRY_FLAGS = {"US": "🇺🇸", "EU": "🇪🇺", "GB": "🇬🇧", "IE": "🇮🇪", "JP": "🇯🇵", "CN": "🇨🇳",
                 "AU": "🇦🇺", "CA": "🇨🇦", "CH": "🇨🇭", "NZ": "🇳🇿"}


# ------------------------------------------------------------------ raw sources
TV_CAP = 2000  # the API silently truncates responses at this many events


def tradingview_events(start: dt.datetime, end: dt.datetime, countries: list[str]) -> list[dict]:
    """Events for the window, one request per country so the response cap is never hit."""
    out = []
    for c in countries:
        r = get("https://economic-calendar.tradingview.com/events",
                params={"from": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "to": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "countries": c},
                headers={"Origin": "https://www.tradingview.com",
                         "Referer": "https://www.tradingview.com/"}, timeout=40)
        j = r.json()
        if j.get("status") != "ok":
            raise ValueError(f"TradingView status {j.get('status')} for {c}")
        if len(j["result"]) >= TV_CAP:
            raise ValueError(f"TradingView response for {c} hit the {TV_CAP}-event cap; shorten the window")
        out.extend(j["result"])
    return out


def forexfactory_week() -> list[dict]:
    return get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=30).json()


def fred_release_dates(release_id: int, start: dt.date) -> set[dt.date]:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise ValueError("FRED_API_KEY not set")
    j = get("https://api.stlouisfed.org/fred/release/dates",
            params={"release_id": release_id, "api_key": key, "file_type": "json",
                    "realtime_start": start.isoformat(), "realtime_end": "9999-12-31",
                    "include_release_dates_with_no_data": "true", "limit": 1000}).json()
    return {dt.date.fromisoformat(r["date"]) for r in j.get("release_dates", [])}


_CSO_TITLE = "Flash Estimate of Harmonised Index of Consumer Prices"
_CSO_URL = ("https://www.cso.ie/en/csolatestnews/pressreleases/{year}pressreleases/"
            "pressstatementflashestimatefortheharmonisedindexofconsumerprices{period}/")


def cso_calendar() -> list[dict]:
    j = get("https://cdn.cso.ie/rc/ReleaseCalendar.json", timeout=40).json()
    out = []
    for r in j.get("releases", []):
        if r.get("title") == _CSO_TITLE and r.get("status") != "Cancelled":
            d = dt.datetime.strptime(r["releasedate"], "%d/%m/%Y").date()
            out.append({"date": d, "refperiod": r["refperiod"]})
    uniq = {(o["date"], o["refperiod"]): o for o in out}
    return sorted(uniq.values(), key=lambda o: o["date"])


def cso_flash_value(refperiod: str, release_years: list[int]) -> tuple[float, str] | None:
    """Parse the annual HICP flash figure from the CSO press statement."""
    slug = refperiod.lower().replace(" ", "")
    for year in release_years:
        url = _CSO_URL.format(year=year, period=slug)
        try:
            text = get(url, timeout=30).text
        except Exception:
            continue
        t = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)))
        m = re.search(r"HICP\)? for Ireland is estimated to have (increased|risen|decreased|fallen|"
                      r"remained unchanged)(?: by)? ?([\d.]+)?% in the 12 months", t)
        if not m:
            m = re.search(r"estimated to have (increased|risen|decreased|fallen) by ([\d.]+)% in the 12 months", t)
        if m:
            if m.group(1) == "remained unchanged":
                return 0.0, url
            v = float(m.group(2))
            return (-v if m.group(1) in ("decreased", "fallen") else v), url
    return None


# ------------------------------------------------------------------ helpers
def parse_num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("%", "").replace("<", "").replace(">", "")
    s = re.sub(r"[KMBT]$", "", s)
    try:
        return float(s)
    except ValueError:
        return None


def fmt_val(v, unit: str | None, scale: str | None) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, str):
        return v
    s = f"{v:,.3f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
    if scale:
        s += scale
    if unit == "%":
        s += "%"
    return s


def _tv_event_fields(e: dict) -> dict:
    return {"actual": e.get("actual"), "forecast": e.get("forecast"),
            "previous": e.get("previous"), "unit": e.get("unit"), "scale": e.get("scale"),
            "period": e.get("period"), "source": "TradingView economic calendar",
            "source_url": e.get("source_url"), "tv_title": e.get("title")}


# ------------------------------------------------------------------ build
def build_calendar(cfg: dict, cb_cfg: dict, state: dict, status: Status) -> tuple[list[dict], list[dict]]:
    ccfg = cfg["calendar"]
    now = now_utc()
    start = now - dt.timedelta(days=ccfg["lookback_days"])
    end = now + dt.timedelta(days=ccfg["lookahead_days"])
    checks: list[dict] = []
    fresh: dict[str, dict] = {}

    try:
        tv = tradingview_events(start, end, ccfg["countries"])
        status.ok("calendar_tv", "TradingView economic calendar", f"{len(tv)} events")
    except Exception as e:
        tv = []
        status.fail("calendar_tv", "TradingView economic calendar", e)

    # --- data releases
    for rel in ccfg["releases"]:
        if rel.get("source") == "cso":
            continue
        for e in tv:
            if e["country"] == rel["country"] and re.search(rel["match"], e["title"]):
                t = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
                key = f"{rel['id']}:{t.strftime('%Y-%m-%dT%H:%M')}"
                fresh[key] = {"key": key, "id": rel["id"], "country": rel["country"],
                              "name": rel["name"], "short": rel.get("short", rel["name"]),
                              "category": rel["category"], "time": t.isoformat(),
                              "kind": "data", **_tv_event_fields(e)}

    # --- central bank decisions and minutes (dates from central_banks.yaml only)
    for b in cb_cfg["banks"]:
        tv_hits = [e for e in tv if e["country"] == b["country"]
                   and re.search(b["calendar_match"], e["title"])]
        for d in b.get("meetings", []):
            d = d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
            rule = b.get("minutes_rule")
            if rule:
                md = d + dt.timedelta(days=rule["days_after"])
                mt = at_local(md, rule["time"]).astimezone(UTC)
                if start <= mt <= end:
                    k = f"cbmin_{b['id']}:{md.isoformat()}"
                    fresh[k] = {"key": k, "id": f"cbmin_{b['id']}", "country": b["country"],
                                "name": f"{b['short']} minutes ({d:%d %b} meeting)",
                                "short": f"{b['short']} minutes", "category": "Central banks",
                                "time": mt.isoformat(), "kind": "minutes", "actual": None,
                                "forecast": None, "previous": None, "unit": None, "scale": None,
                                "source": f"Derived from central_banks.yaml: published {rule['days_after']} days after the meeting"}
            t = at_local(d, b["decision_time"]).astimezone(UTC)
            if not (start <= t <= end):
                continue
            ev = {"key": f"cb_{b['id']}:{d.isoformat()}", "id": f"cb_{b['id']}",
                  "country": b["country"], "name": f"{b['name']} rate decision",
                  "short": f"{b['short']} decision", "category": "Central banks",
                  "time": t.isoformat(), "kind": "cb", "actual": None, "forecast": None,
                  "previous": None, "unit": "%", "scale": None,
                  "source": "central_banks.yaml (date); time is the usual announcement time",
                  "bank": b["id"]}
            match = [e for e in tv_hits
                     if abs((dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")) - t).total_seconds()) < 36 * 3600]
            if match:
                e = match[0]
                ev.update(_tv_event_fields(e))
                ev["time"] = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).isoformat()
                ev["source"] = "central_banks.yaml (date) + TradingView (time, consensus, actual)"
                ev["unit"] = "%"
            fresh[ev["key"]] = ev

    # --- Irish HICP flash from CSO
    rel = next((r for r in ccfg["releases"] if r.get("source") == "cso"), None)
    if rel:
        try:
            cal = cso_calendar()
            for r in cal:
                t = dt.datetime.combine(r["date"], dt.time(11, 0), tzinfo=DUBLIN).astimezone(UTC)
                if not (start <= t <= end):
                    continue
                key = f"{rel['id']}:{t.strftime('%Y-%m-%dT%H:%M')}"
                old = state.get("events", {}).get(key, {})
                ev = {"key": key, "id": rel["id"], "country": "IE", "name": rel["name"],
                      "short": rel["short"], "category": rel["category"], "time": t.isoformat(),
                      "kind": "data", "unit": "%", "scale": None, "period": r["refperiod"],
                      "forecast": None, "forecast_note": "No free consensus forecast for Irish HICP flash",
                      "actual": old.get("actual"), "previous": old.get("previous"),
                      "source": "CSO release calendar + CSO press statement",
                      "source_url": "https://www.cso.ie/en/csolatestnews/releasecalendar/"}
                if ev["previous"] is None:
                    ref = dt.datetime.strptime(r["refperiod"], "%B %Y")
                    pm = (ref.replace(day=1) - dt.timedelta(days=1))
                    prev = cso_flash_value(pm.strftime("%B %Y"), [pm.year, pm.year + 1])
                    if prev:
                        ev["previous"] = prev[0]
                if ev["actual"] is None and t <= now:
                    act = cso_flash_value(r["refperiod"], [r["date"].year])
                    if act:
                        ev["actual"], ev["source_url"] = act
                fresh[key] = ev
            status.ok("calendar_cso", "CSO Ireland")
        except Exception as e:
            status.fail("calendar_cso", "CSO Ireland", e)

    # --- Forex Factory: cross-check times, fill missing consensus
    try:
        ff = forexfactory_week()
        status.ok("calendar_ff", "Forex Factory weekly JSON", f"{len(ff)} events")
        ff_by = {}
        for f in ff:
            c = FF_CCY.get(f.get("country"))
            if c:
                ff_by.setdefault((c, f["title"]), []).append(f)
        ff_titles = {r["id"]: r.get("ff") for r in ccfg["releases"]}
        ff_titles.update({f"cb_{b['id']}": b.get("ff") for b in cb_cfg["banks"]})
        for ev in fresh.values():
            title = ff_titles.get(ev["id"])
            if not title:
                continue
            t = dt.datetime.fromisoformat(ev["time"])
            cands = [(f, dt.datetime.fromisoformat(f["date"])) for f in ff_by.get((ev["country"], title), [])]
            cands = [(f, ft) for f, ft in cands if abs((ft - t).total_seconds()) < 36 * 3600]
            if not cands:
                continue
            f, ft = min(cands, key=lambda x: abs((x[1] - t).total_seconds()))
            diff_min = round((ft - t).total_seconds() / 60)
            checks.append({"what": f"{COUNTRY_NAMES[ev['country']]} {ev['short']}",
                           "source": "Forex Factory", "ok": abs(diff_min) <= 5,
                           "detail": "time matches" if abs(diff_min) <= 5 else
                           f"time differs by {diff_min:+d} min (FF {ft.astimezone(DUBLIN):%a %H:%M})"})
            if ev.get("forecast") is None and f.get("forecast"):
                ev["forecast"] = f["forecast"]
                ev["forecast_source"] = "Forex Factory"
            if ev.get("previous") is None and f.get("previous"):
                ev["previous"] = f["previous"]
    except Exception as e:
        status.fail("calendar_ff", "Forex Factory weekly JSON", e)

    # --- FRED: cross-check US release dates
    if os.environ.get("FRED_API_KEY"):
        for rel in ccfg["releases"]:
            rid = rel.get("fred_release")
            evs = [ev for ev in fresh.values() if ev["id"] == rel["id"]]
            if not rid or not evs:
                continue
            try:
                dates = fred_release_dates(rid, start.date())
                for ev in evs:
                    d = dt.datetime.fromisoformat(ev["time"]).astimezone(ZoneInfo("America/New_York")).date()
                    ok = d in dates
                    checks.append({"what": f"US {ev['short']} {d:%d %b}", "source": f"FRED release {rid}",
                                   "ok": ok, "detail": "date confirmed" if ok else "date not in FRED release schedule"})
            except Exception as e:
                status.fail(f"fred_release_{rid}", "FRED release dates", e)

    # --- merge with stored state (keeps actuals and reactions across runs)
    stored = state.setdefault("events", {})
    for key, ev in fresh.items():
        old = stored.get(key, {})
        for fld in ("actual", "forecast", "previous"):
            if ev.get(fld) is None and old.get(fld) is not None:
                ev[fld] = old[fld]
        if "reaction" in old:
            ev["reaction"] = old["reaction"]
        stored[key] = ev
    horizon = now - dt.timedelta(days=ccfg["keep_released_days"] + 7)
    for key in [k for k, v in stored.items() if dt.datetime.fromisoformat(v["time"]) < horizon]:
        del stored[key]
    # drop stored future events the sources no longer list (e.g. rescheduled) -
    # only when the calendar fetch worked, so an outage never empties the page
    if tv:
        for key in [k for k, v in stored.items()
                    if key_is_future(v, now) and k not in fresh and v.get("id") != "ie_hicp"]:
            del stored[key]
        # ...and past releases that never got a figure and the source has since
        # withdrawn (e.g. a y/y rate not published because the year-ago figure
        # doesn't exist), so they don't sit on the page as "not reported yet".
        for key in [k for k, v in stored.items()
                    if v.get("kind") == "data" and v.get("id") != "ie_hicp" and k not in fresh
                    and v.get("actual") is None and not key_is_future(v, now)
                    and now - dt.datetime.fromisoformat(v["time"]) > dt.timedelta(hours=3)]:
            del stored[key]

    events = sorted(stored.values(), key=lambda e: e["time"])
    for ev in events:
        a, f = parse_num(ev.get("actual")), parse_num(ev.get("forecast"))
        ev["surprise"] = round(a - f, 4) if a is not None and f is not None else None
        ev["actual_str"] = fmt_val(ev.get("actual"), ev.get("unit"), ev.get("scale"))
        ev["forecast_str"] = fmt_val(ev.get("forecast"), ev.get("unit"), ev.get("scale"))
        ev["previous_str"] = fmt_val(ev.get("previous"), ev.get("unit"), ev.get("scale"))
        ev["surprise_str"] = (fmt_val(ev["surprise"], ev.get("unit"), ev.get("scale"))
                              if ev["surprise"] is not None else "n/a")
        if ev["surprise"] is not None and ev["surprise"] > 0:
            ev["surprise_str"] = "+" + ev["surprise_str"]
        ev["country_name"] = COUNTRY_NAMES.get(ev["country"], ev["country"])
        ev["flag"] = COUNTRY_FLAGS.get(ev["country"], "")
        ev["label"] = ev["name"] if ev["kind"] in ("cb", "minutes") else f"{ev['country_name']} {ev['name']}"
    return events, checks


def key_is_future(ev: dict, now: dt.datetime) -> bool:
    return dt.datetime.fromisoformat(ev["time"]) > now


# ------------------------------------------------------------------ market reactions
def compute_reactions(events: list[dict], cfg: dict, hist, live: dict) -> None:
    """Move from the previous day's close in the asset(s) that matter for each release.
    Uses that day's close once stored, else the latest intraday quote taken after
    the release (from `live`, as returned by markets.live_quotes)."""

    ccfg = cfg["calendar"]
    now = now_utc()
    specs = {s["id"]: s for s in cfg["series"]}
    rel_assets = {r["id"]: r.get("reaction") for r in ccfg["releases"]}
    for ev in events:
        t = dt.datetime.fromisoformat(ev["time"])
        if t > now or now - t > dt.timedelta(days=ccfg["keep_released_days"]) or ev["kind"] == "minutes":
            continue
        assets = rel_assets.get(ev["id"]) or ccfg["reaction_defaults"].get(ev["country"], [])
        reaction = dict(ev.get("reaction") or {})
        rel_day = t.astimezone(DUBLIN).date()
        for sid in assets:
            spec = specs.get(sid)
            if not spec or reaction.get(sid, {}).get("basis") == "close":
                continue
            rows = hist.series(sid)
            if rows.empty:
                continue
            before = rows[rows["date"] < rel_day]
            if before.empty:
                continue
            prev = float(before["value"].iloc[-1])
            on = rows[rows["date"] == rel_day]
            entry = {"name": spec["name"], "prev": prev, "prev_date": before["date"].iloc[-1].isoformat(),
                     "yield": spec["group"] == "yields", "change": None, "value": None}
            if not on.empty:
                entry.update(value=float(on["value"].iloc[0]), basis="close")
            elif sid in live and live[sid]["at"] >= t:
                q = live[sid]
                entry.update(value=q["value"], basis="intraday", at=q["at"].astimezone(DUBLIN).strftime("%H:%M"))
            else:
                entry.update(value=None, basis="pending")
            if entry.get("value") is not None:
                entry["change"] = ((entry["value"] - prev) * 100 if entry["yield"]
                                   else (entry["value"] / prev - 1) * 100)
            reaction[sid] = entry
        if reaction:
            ev["reaction"] = reaction
