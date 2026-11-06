"""Central bank policy rates from official APIs, with a hand-edited YAML for the rest."""
from __future__ import annotations

import datetime as dt
import io
import logging
import re

import pandas as pd

from .econ_calendar import tradingview_events
from .markets import boe_series, fred_series
from .util import Status, at_local, get, now_utc

log = logging.getLogger("dashboard")
YAML_MAX_AGE_DAYS = 60


def _last_change(s: pd.Series) -> tuple[float, dt.date | None, float | None, dt.date]:
    """(current, date of last change, size in bp, as-of date) from a level series."""
    s = s.dropna().sort_index()
    cur = float(s.iloc[-1])
    changed = s[s.diff().fillna(0).abs() > 1e-9]
    if changed.empty:
        return cur, None, None, s.index[-1]
    d = changed.index[-1]
    prev = float(s[s.index < d].iloc[-1])
    return cur, d, round((cur - prev) * 100), s.index[-1]


def _fed() -> tuple[pd.Series, str]:
    try:
        return fred_series("DFEDTARU", 1100), "FRED (DFEDTARU, target range upper bound)"
    except Exception as e:
        log.info("FRED unavailable for Fed rate (%s); using NY Fed", e)
    j = get("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/750.json").json()
    out = {dt.date.fromisoformat(r["effectiveDate"]): float(r["targetRateTo"])
           for r in j["refRates"] if r.get("targetRateTo") is not None}
    return pd.Series(out, dtype=float).sort_index(), "NY Fed (EFFR release, target range upper bound)"


def _ecb() -> tuple[pd.Series, str]:
    r = get("https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV",
            params={"format": "csvdata", "startPeriod": "2015-01-01"})
    df = pd.read_csv(io.StringIO(r.text))
    s = pd.Series(df["OBS_VALUE"].astype(float).values,
                  index=pd.to_datetime(df["TIME_PERIOD"]).dt.date.values).sort_index()
    return s, "ECB Data Portal (FM.D.U2.EUR.4F.KR.DFR.LEV, effective dates)"


def _boe() -> tuple[pd.Series, str]:
    return boe_series("IUDBEDR", dt.date(2015, 1, 1)), "Bank of England database (IUDBEDR)"


def _snb() -> tuple[pd.Series, str]:
    r = get("https://data.snb.ch/api/cube/snbgwdzid/data/csv/en", timeout=60)
    rows = {}
    for line in r.content.decode("utf-8-sig").splitlines():
        p = [x.strip('"') for x in line.split(";")]
        if len(p) == 3 and p[1] == "LZ" and p[2] not in ("",):
            rows[dt.date.fromisoformat(p[0])] = float(p[2])
    return pd.Series(rows, dtype=float).sort_index(), "SNB data portal (snbgwdzid, LZ)"


def _boc() -> tuple[pd.Series, str]:
    j = get("https://www.bankofcanada.ca/valet/observations/V39079/json",
            params={"start_date": "2015-01-01"}).json()
    rows = {dt.date.fromisoformat(o["d"]): float(o["V39079"]["v"]) for o in j["observations"]
            if o.get("V39079", {}).get("v") not in (None, "")}
    return pd.Series(rows, dtype=float).sort_index(), "Bank of Canada Valet API (V39079)"


FETCHERS = {"fred_range": _fed, "ecb": _ecb, "boe": _boe, "snb": _snb, "boc": _boc}


def meeting_datetimes(bank: dict) -> list[dt.datetime]:
    return [at_local(d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d)),
                     bank["decision_time"]).astimezone(dt.timezone.utc)
            for d in bank.get("meetings", [])]


def build_rates(cb_cfg: dict, status: Status) -> dict:
    now = now_utc()
    today = now.date()
    reviewed = cb_cfg.get("last_reviewed")
    reviewed = reviewed if isinstance(reviewed, dt.date) else dt.date.fromisoformat(str(reviewed))
    age = (today - reviewed).days
    warnings = []
    if age > YAML_MAX_AGE_DAYS:
        warnings.append(f"central_banks.yaml was last reviewed {age} days ago ({reviewed}). "
                        "Meeting dates and manually entered rates may be out of date.")

    # Latest decisions on the calendar, used to cross-check every bank
    tv_latest = {}
    try:
        countries = sorted({b["country"] for b in cb_cfg["banks"]})
        evs = tradingview_events(now - dt.timedelta(days=75), now + dt.timedelta(days=1), countries)
        evs = [e for e in evs if e["date"] <= now.strftime("%Y-%m-%dT%H:%M:%S")]
        for b in cb_cfg["banks"]:
            hits = [e for e in evs if e["country"] == b["country"] and e.get("actual") is not None
                    and re.search(b["calendar_match"], e["title"])]
            if hits:
                tv_latest[b["id"]] = max(hits, key=lambda e: e["date"])
    except Exception as e:
        status.fail("rates_crosscheck", "TradingView calendar", e)

    banks = []
    for b in cb_cfg["banks"]:
        row = {k: b.get(k) for k in ("id", "name", "short", "country", "rate_label")}
        row["warnings"] = []
        src = b["rate_source"]
        try:
            if src == "manual":
                m = b["manual"]
                lc = m.get("last_change")
                row.update(rate=float(m["rate"]),
                           last_change=lc.isoformat() if isinstance(lc, dt.date) else lc,
                           last_change_bp=m.get("last_change_bp"),
                           as_of=reviewed.isoformat(),
                           source=f"central_banks.yaml (hand-maintained, reviewed {reviewed})",
                           manual=True)
                status.ok(f"rate_{b['id']}", "central_banks.yaml")
            else:
                s, label = FETCHERS[src]()
                cur, lc, bp, asof = _last_change(s)
                row.update(rate=cur, last_change=lc.isoformat() if lc else None,
                           last_change_bp=bp, as_of=asof.isoformat(), source=label, manual=False)
                status.ok(f"rate_{b['id']}", label)
        except Exception as e:
            status.fail(f"rate_{b['id']}", src, e)
            row.update(rate=None, source=src, error=str(e)[:200])

        tv = tv_latest.get(b["id"])
        if tv and abs(float(tv["actual"]) - (row.get("rate") if row.get("rate") is not None else -99)) > 1e-6:
            tv_day = tv["date"][:10]
            # A decision announced after the source's last change: the official
            # series (or the YAML) just hasn't caught up yet, so show the announcement.
            if row.get("rate") is None or not row.get("last_change") or tv_day >= row["last_change"]:
                old = row.get("rate")
                note = (f"{row['source']} still shows {old:.2f}% (as of {row.get('as_of')})"
                        if old is not None else f"{row['source']} unavailable")
                row.update(rate=float(tv["actual"]), last_change=tv_day,
                           last_change_bp=round((float(tv["actual"]) - old) * 100) if old is not None else None,
                           as_of=tv_day, announced=True, error=None,
                           source=f"Announced decision of {tv_day} (TradingView calendar); {note}")
                if row.get("manual"):
                    row["warnings"].append(f"Rate taken from the {tv_day} announcement. Update central_banks.yaml.")
            else:
                row["warnings"].append(
                    f"Calendar shows the {tv_day} decision at {tv['actual']}% "
                    f"but this row says {row['rate']}%." + (" Update central_banks.yaml." if row.get("manual") else ""))
        lc = row.get("last_change")
        row["recent_change"] = bool(lc and row.get("last_change_bp") and
                                    (today - dt.date.fromisoformat(lc[:10])).days <= 3)

        mts = meeting_datetimes(b)
        future = [m for m in mts if m > now - dt.timedelta(hours=2)]
        past = [m for m in mts if m <= now]
        row["next_meeting"] = future[0].isoformat() if future else None
        row["last_meeting"] = past[-1].isoformat() if past else None
        if not future:
            row["warnings"].append("No future meeting date in central_banks.yaml - please add the next dates.")
        banks.append(row)
    return {"banks": banks, "warnings": warnings, "last_reviewed": reviewed.isoformat()}
