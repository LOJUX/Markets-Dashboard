"""Changes, volatility-scaled big movers and staleness, computed from the stored history."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .store import History
from .util import last_completed_session, now_utc, parse_local_time


def _expected_session(spec: dict) -> dt.date:
    if spec.get("weekends"):
        t, tz = parse_local_time(spec["close"])
        local = now_utc().astimezone(tz)
        return local.date() - (dt.timedelta(days=1) if local.time() < t else dt.timedelta(0))
    return last_completed_session(spec["close"])


def session_of(ts: dt.datetime, spec: dict) -> dt.date:
    """Trading session a quote belongs to: a quote after the session close
    (e.g. FX or futures trading overnight) counts towards the next session."""
    t, tz = parse_local_time(spec["close"])
    local = ts.astimezone(tz)
    d = local.date()
    if spec.get("weekends"):
        return d
    if local.time() > t:
        d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def _value_on_or_before(s: pd.Series, d: dt.date):
    sub = s[s.index <= d]
    return (float(sub.iloc[-1]), sub.index[-1]) if not sub.empty else (None, None)


def _change(cur: float, base: float | None, is_yield: bool):
    if base is None or cur is None:
        return None
    if is_yield:
        return (cur - base) * 100.0
    return (cur / base - 1.0) * 100.0 if base else None


def analyse(cfg: dict, hist: History, status: dict, live: dict | None = None) -> dict:
    live = live or {}
    mcfg = cfg["movers"]
    stale_tol = cfg.get("stale_after_business_days", 1)
    out = {}
    for spec in cfg["series"]:
        sid = spec["id"]
        is_yield = spec["group"] == "yields"
        rows = hist.series(sid)
        st = status.get(sid, {})
        item = {"id": sid, "name": spec["name"], "group": spec["group"],
                "region": spec.get("region"), "unit": spec.get("unit"),
                "is_yield": is_yield, "fetch_ok": st.get("ok", False),
                "fetch_error": None if st.get("ok") else st.get("detail"),
                "fetched_at": st.get("at")}
        if rows.empty:
            item.update(available=False)
            out[sid] = item
            continue
        # Use only rows from the same source as the latest value so a fallback
        # source never produces a fake jump in the changes or sparkline.
        latest_src = rows["source"].iloc[-1]
        rows = rows[rows["source"] == latest_src]
        s = pd.Series(rows["value"].values, index=rows["date"].values, dtype=float)
        last_d = s.index[-1]

        # volatility: stdev of own daily close-to-close changes over the last year
        yr = s[s.index > last_d - dt.timedelta(days=365)]
        daily = (yr.diff() * 100.0) if is_yield else (yr.pct_change() * 100.0)
        daily = daily.replace([np.inf, -np.inf], np.nan).dropna()

        # A live quote from a session newer than the last stored close is the
        # current value; the move is then measured from that last close.
        lq = live.get(sid)
        lq_session = session_of(lq["at"], spec) if lq else None
        if lq and lq_session > last_d:
            basis, cur, prev, ref_d, cur_d = "live", lq["value"], float(s.iloc[-1]), last_d, lq_session
            src_label, at = lq["source"], lq["at"].isoformat()
            sd_sample = daily
        else:
            basis, cur, cur_d, src_label, at = "close", float(s.iloc[-1]), last_d, latest_src, None
            prev = float(s.iloc[-2]) if len(s) > 1 else None
            ref_d = s.index[-2] if len(s) > 1 else None
            sd_sample = daily.iloc[:-1]  # exclude the move being judged
        w, _ = _value_on_or_before(s, cur_d - dt.timedelta(days=7))
        m, _ = _value_on_or_before(s, cur_d - dt.timedelta(days=30))
        y, y_d = _value_on_or_before(s, dt.date(cur_d.year - 1, 12, 31))
        if y_d is not None and y_d < dt.date(cur_d.year - 1, 12, 20):
            y = None  # history does not reach the previous year-end
        chg1 = _change(cur, prev, is_yield)
        sd = float(sd_sample.std()) if len(sd_sample) > 60 else None
        z = chg1 / sd if (chg1 is not None and sd) else None

        expected = _expected_session(spec)
        seen = max(last_d, lq_session) if lq_session else last_d
        gap = int(np.busday_count(seen, expected)) if not spec.get("weekends") else (expected - seen).days
        stale = gap > stale_tol

        thr = spec.get("mover_bp" if is_yield else "mover_pct", mcfg["thresholds"][spec["group"]])
        reasons = []
        if chg1 is not None and not stale:
            if abs(chg1) >= thr:
                reasons.append(f"beyond ±{thr:g}{'bp' if is_yield else '%'}")
            if z is not None and abs(z) >= mcfg["sigma"]:
                reasons.append(f"{abs(z):.1f}σ move")

        spark = [float(v) for v in s[s.index > cur_d - dt.timedelta(days=92)].values]
        chart = [[d.isoformat(), round(float(v), 6)] for d, v in yr.items()]
        if basis == "live":
            spark.append(cur)
            chart.append([cur_d.isoformat(), round(cur, 6)])
        item.update(
            available=True, value=cur, date=cur_d.isoformat(), source=src_label, basis=basis, at=at,
            ref_date=ref_d.isoformat() if ref_d else None, close_source=latest_src,
            prev=prev, chg_1d=chg1, chg_1w=_change(cur, w, is_yield), chg_1m=_change(cur, m, is_yield),
            chg_ytd=_change(cur, y, is_yield), sd=sd, z=z, stale=stale, expected=expected.isoformat(),
            mover=bool(reasons), mover_reasons=reasons, n_points=len(s),
            spark=[round(v, 6) for v in spark], chart=chart,
        )
        out[sid] = item

    # US 2s10s spread (bp), from same-date closes plus the current values
    a, b = hist.series("US2Y"), hist.series("US10Y")
    if not a.empty and not b.empty and out.get("US2Y", {}).get("available") and out.get("US10Y", {}).get("available"):
        sa = pd.Series(a["value"].values, index=a["date"].values)
        sb = pd.Series(b["value"].values, index=b["date"].values)
        sp = ((sb - sa) * 100).dropna().sort_index()
        two, ten = out["US2Y"], out["US10Y"]
        if len(sp):
            sp = sp[sp.index > sp.index[-1] - dt.timedelta(days=365)]
            cur = (ten["value"] - two["value"]) * 100
            live_now = "live" in (two["basis"], ten["basis"])
            prev = float(sp.iloc[-1]) if live_now else (float(sp.iloc[-2]) if len(sp) > 1 else None)
            chart = [[d.isoformat(), round(float(v), 2)] for d, v in sp.items()]
            if live_now:
                chart.append([max(two["date"], ten["date"]), round(cur, 2)])
            out["_2s10s"] = {"id": "_2s10s", "name": "US 2s10s spread", "value": cur,
                             "date": max(two["date"], ten["date"]),
                             "chg_1d": cur - prev if prev is not None else None,
                             "basis": "live" if live_now else "close", "is_yield": True,
                             "at": max((x["at"] for x in (two, ten) if x.get("at")), default=None),
                             "source": f"US10Y ({ten['source']}) minus US2Y ({two['source']})",
                             "chart": chart}
    return out
