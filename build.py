#!/usr/bin/env python3
"""Build the global markets dashboard (run every 15 minutes by GitHub Actions).

    python build.py      # fetch closes, intraday quotes, rates, calendar, news; write site/index.html
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import logging
import os
import sys

import yaml

from dashboard.analytics import analyse
from dashboard.econ_calendar import build_calendar, compute_reactions
from dashboard.markets import live_quotes, run_cross_checks, update_history
from dashboard.news import fetch_news
from dashboard.rates import build_rates
from dashboard.render import render
from dashboard.store import History
from dashboard.summary import summarise
from dashboard.util import DUBLIN, Status, now_utc

ROOT = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("dashboard")


def load_yaml(path: str) -> dict:
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json(path: str, default):
    p = os.path.join(ROOT, path)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, obj) -> None:
    p = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str)


def pick_movers(cfg: dict, series: dict) -> list[dict]:
    """Unusual moves, most unusual first. The headline markets in the Key markets
    panel get a small boost, so a 2.1σ S&P move outranks a 2.3σ wheat move."""
    key = {sid for ids in cfg.get("key_markets", {}).values() for sid in ids}
    m = [v for k, v in series.items() if not k.startswith("_") and v.get("mover")]
    return sorted(m, key=lambda v: v.get("z_rank", 0) * (1.2 if v["id"] in key else 1.0), reverse=True)


def context(cfg, series, rates, events, cal_checks, news, summary, summary_source,
            market_checks, status) -> dict:
    now = now_utc()
    ccfg = cfg["calendar"]
    items = {k: v for k, v in series.items() if not k.startswith("_")}
    groups = {g: [series[s["id"]] for s in cfg["series"] if s["group"] == g] for g in cfg["groups"]}
    if series.get("_2s10s"):
        groups["yields"].append({**series["_2s10s"], "available": True, "fetch_ok": True, "stale": False,
                                 "group": "yields", "spread": True, "z": None, "z_w": None,
                                 "chg_1w": None, "mover": False, "mover_reasons": []})

    # What just changed: releases and decisions from the last 36 hours up front,
    # the rest of the keep window behind a disclosure.
    keep = dt.timedelta(days=ccfg["keep_released_days"])
    released = [e for e in events if now - keep <= dt.datetime.fromisoformat(e["time"]) <= now]
    released.sort(key=lambda e: e["time"], reverse=True)
    for e in released:
        if e["kind"] == "cb" and e.get("actual") is not None and e.get("previous") is not None:
            e["move_bp"] = round((e["actual"] - e["previous"]) * 100)
    fresh = [e for e in released if now - dt.datetime.fromisoformat(e["time"]) <= dt.timedelta(hours=36)]
    older = [e for e in released if e not in fresh]
    # Coming up and the countdown strip show only the big releases: second-tier
    # data (`minor: true` in config) and minutes from banks outside key_banks are left out.
    minor_ids = {r["id"] for r in ccfg["releases"] if r.get("minor")}
    key_banks = cfg.get("key_banks", [])

    def major(e):
        if e["kind"] == "minutes":
            return e["id"].removeprefix("cbmin_") in key_banks
        return e["id"] not in minor_ids
    week = [e for e in events if major(e) and
            now < dt.datetime.fromisoformat(e["time"]) <= now + dt.timedelta(days=ccfg["countdown_days"])]

    js_fields = ("key", "time", "short", "name", "label", "country", "country_name", "flag", "kind",
                 "forecast_str", "previous_str", "actual_str", "actual")
    events_js = [{k: e.get(k) for k in js_fields} for e in events
                 if major(e) and dt.datetime.fromisoformat(e["time"]) > now - dt.timedelta(days=1)]

    movers = pick_movers(cfg, series)
    shown = cfg["movers"].get("max_shown", 5)
    # nothing unusual: show the moves closest to unusual instead, so the box is never empty
    def closeness(v):
        return max(abs(v.get("z") or 0), abs(v.get("z_w") or 0))
    near = [] if movers else sorted((v for v in items.values() if (v.get("z") is not None or v.get("z_w") is not None)
                                     and not v.get("stale")), key=closeness, reverse=True)[:3]

    # What's going on: the fixed headline markets, and the main central banks
    key_panel = [(title, [series[sid] for sid in ids if sid in series])
                 for title, ids in cfg.get("key_markets", {}).items()]
    by_id = {b["id"]: b for b in rates["banks"]}
    key_rates = [by_id[i] for i in key_banks if i in by_id]

    failed = [{"key": k, **v} for k, v in status.items() if not v.get("ok")]
    warnings = []
    stale = [v["name"] for v in items.values() if v.get("available") and v.get("stale")]
    missing = [v["name"] for v in items.values() if not v.get("available")]
    if missing:
        warnings.append("Data unavailable: " + ", ".join(missing) + ".")
    if stale:
        warnings.append("Stale (no recent close): " + ", ".join(stale) + ". Last known values are shown and labelled.")
    warnings += rates.get("warnings", [])
    for b in rates["banks"]:
        warnings += [f"{b['short']}: {w}" for w in b["warnings"]]
    if failed:
        warnings.append(f"{len(failed)} source request(s) failed this run (details under Data checks).")

    chart_js = {k: {"name": v["name"], "is_yield": v["is_yield"], "chart": v.get("chart", []),
                    "source": v.get("source", "")}
                for k, v in series.items() if v.get("chart")}
    return dict(
        generated_at=now.isoformat(), warnings=warnings, refresh=cfg.get("refresh", {}),
        summary=summary, summary_source=summary_source, movers=movers[:shown],
        more_movers=movers[shown:], near=near, key_panel=key_panel, key_rates=key_rates,
        mcfg=cfg["movers"],
        fresh=fresh, older=older, week=week,
        rate_changes=[b for b in rates["banks"] if b.get("recent_change") and
                      (now.date() - dt.date.fromisoformat(b["last_change"][:10])).days <= 1],
        calendar_ok=status.get("calendar_tv", {}).get("ok", False),
        cal_cfg=ccfg, cal_js={k: ccfg[k] for k in ("countdown_days", "countdown_count", "amber_hours")},
        events_js=events_js, rates=rates, groups=[
            ("Bonds", groups["yields"]), ("Currencies", groups["fx"]),
            ("Commodities", groups["commodities"]), ("Equities", groups["equities"])],
        news=news, market_checks=market_checks,
        calendar_checks=cal_checks, failed_sources=failed, chart_js=chart_js,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml", help="config file (for testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("yfinance", "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    cfg = load_yaml(args.config)
    paths = cfg["paths"]
    cb_cfg = load_yaml(paths["central_banks"])
    hist = History(os.path.join(ROOT, paths["history"]))
    state = load_json(paths["calendar_state"], {"events": {}})
    status = Status()

    log.info("== markets")
    update_history(cfg, hist, status)
    hist.save()
    live = live_quotes(cfg, hist, status)
    log.info("== rates")
    rates = build_rates(cb_cfg, status)
    log.info("== news")
    news = fetch_news(cfg, status)
    log.info("== cross-checks")
    market_checks = run_cross_checks(cfg, hist)

    series = analyse(cfg, hist, status.to_dict(), live)
    log.info("== calendar")
    events, cal_checks = build_calendar(cfg, cb_cfg, state, status)
    compute_reactions(events, cfg, hist, live)
    # Persist only end-of-day reactions (intraday ones are recomputed every run), so
    # the committed state changes only when something new is final.
    saved = copy.deepcopy(state)
    for ev in events:
        closed = {k: v for k, v in (ev.get("reaction") or {}).items() if v.get("basis") == "close"}
        if closed:
            saved["events"][ev["key"]]["reaction"] = closed
        else:
            saved["events"][ev["key"]].pop("reaction", None)
    save_json(paths["calendar_state"], saved)

    movers = pick_movers(cfg, series)[:cfg["movers"].get("max_shown", 5)]
    cache = load_json(paths["summary_cache"], {})
    summary, summary_source, cache = summarise(cfg, series, movers, events, news, rates, cache)
    save_json(paths["summary_cache"], cache)

    ctx = context(cfg, series, rates, events, cal_checks, news, summary, summary_source,
                  market_checks, status.to_dict())
    out = os.path.join(ROOT, paths["output"])
    render(ctx, out)
    log.info("wrote %s (%d KB)", out, os.path.getsize(out) // 1024)
    fails = [k for k, v in status.to_dict().items() if not v.get("ok")]
    if fails:
        log.warning("%d source(s) failed: %s", len(fails), ", ".join(fails))
    return 0


if __name__ == "__main__":
    sys.exit(main())
