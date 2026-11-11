"""'What's going on' briefing: Claude API if ANTHROPIC_API_KEY is set, otherwise rule-based."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os

from .util import DUBLIN, now_utc

log = logging.getLogger("dashboard")
MODEL = "claude-opus-5-5"


def _pct(v):
    return f"{v:+.1f}%"


def _bp(v):
    return f"{v:+.0f}bp"


def _mv(it, v=None):
    v = it["chg_1d"] if v is None else v
    return _bp(v) if it["is_yield"] else _pct(v)


def _lvl(it):
    v = it["value"]
    if it["is_yield"]:
        return f"{v:.2f}%"
    if it["group"] == "fx" and it["id"] != "DXY":
        return f"{v:.4f}" if v < 20 else f"{v:.2f}"
    unit = it.get("unit") or ""
    num = f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"
    return f"${num}" if unit.startswith("$") else num


def _direction(vals, flat):
    up = sum(v > flat for v in vals)
    down = sum(v < -flat for v in vals)
    if up and not down:
        return "higher"
    if down and not up:
        return "lower"
    return "mixed" if up and down else "little changed"


def _recent_rate_changes(rates):
    out = []
    for b in (rates or {}).get("banks", []):
        if b.get("recent_change") and (now_utc().date() - dt.date.fromisoformat(b["last_change"][:10])).days <= 1:
            verb = "raised" if b["last_change_bp"] > 0 else "cut"
            d = dt.date.fromisoformat(b["last_change"][:10])
            out.append(f"the {b['short']} {verb} {b['rate_label']} {abs(b['last_change_bp'])}bp "
                       f"to {b['rate']:.2f}% on {d:%a %d %b}")
    return out


def rule_based(cfg: dict, series: dict, movers: list[dict], events: list[dict], news: list[dict],
               rates: dict | None = None) -> list[str]:
    """A short briefing: stocks, rates and the dollar, commodities and risk,
    what's driving it, and what's next."""
    ok = {k: v for k, v in series.items() if not k.startswith("_") and v.get("available")
          and v.get("chg_1d") is not None and not v.get("stale")}
    km = cfg.get("key_markets", {})
    out = []

    # 1. Stocks around the world
    eq = [ok[k] for k in km.get("Stocks", []) if k in ok]
    if eq:
        listed = ", ".join(v["name"] + " " + _pct(v["chg_1d"]) for v in eq)
        s = f"Stocks are {_direction([v['chg_1d'] for v in eq], 0.15)} ({listed})"
        wk = [v["chg_1w"] for v in eq if v.get("chg_1w") is not None]
        if wk:
            avg = sum(wk) / len(wk)
            s += (f" and {'up' if avg > 0 else 'down'} {abs(avg):.1f}% on average over the past week"
                  if abs(avg) >= 0.3 else ", roughly flat over the past week")
        out.append(s + ".")

    # 2. Rates and the dollar
    parts = []
    if "US10Y" in ok:
        t = ok["US10Y"]
        wk = t.get("chg_1w")
        trend = ("rising" if wk is not None and wk >= 5 else "falling" if wk is not None and wk <= -5 else "steady")
        parts.append(f"Yields are {trend}: the US 10-year is {t['value']:.2f}% ({_bp(t['chg_1d'])}"
                     + (f", {_bp(wk)} over the week)" if wk is not None else ")"))
    others = [ok[k] for k in ("US2Y", "DE10Y", "GB10Y") if k in ok]
    if others:
        parts.append(", ".join(f"{v['name'].replace('Germany', 'Bund').replace('UK 10Y', 'gilt 10Y')} "
                               f"{v['value']:.2f}% ({_bp(v['chg_1d'])})" for v in others))
    if "DXY" in ok and ok["DXY"].get("chg_1w") is not None:
        d = ok["DXY"]
        parts.append(f"the dollar index is {'up' if d['chg_1w'] >= 0 else 'down'} {abs(d['chg_1w']):.1f}% over the week")
    if parts:
        out.append("; ".join(parts) + ".")

    # 3. Commodities and risk appetite
    bits = []
    for k in ("BRENT", "GOLD", "BTC"):
        if k in ok:
            v = ok[k]
            moves = [_pct(v["chg_1d"])] if abs(v["chg_1d"]) >= 0.05 else []
            if v.get("chg_1w") is not None:
                moves.append(f"{_pct(v['chg_1w'])} on the week")
            bits.append(f"{v['name']} {_lvl(v)}" + (f" ({', '.join(moves)})" if moves else ""))
    if "VIX" in ok:
        x = ok["VIX"]["value"]
        mood = "calm" if x < 15 else "normal" if x < 20 else "elevated" if x < 30 else "stressed"
        bits.append(f"the VIX is {x:.1f} ({mood})")
    if bits:
        s = ", ".join(bits)
        out.append(s[0].upper() + s[1:] + ".")

    # 4. What's driving it: decisions, data surprises, unusual moves
    parts = _recent_rate_changes(rates)
    now = now_utc()
    surprises = [e for e in events if e.get("surprise") and e["kind"] == "data"
                 and now - dt.timedelta(hours=24) <= dt.datetime.fromisoformat(e["time"]) <= now]
    if surprises:
        e = surprises[-1]
        parts.append(f"{e['label']} came in at {e['actual_str']} against {e['forecast_str']} expected")
    if movers:
        def why(m):
            if m.get("horizon") == "week" and m.get("chg_1w") is not None:
                return f"{m['name']} {_mv(m, m['chg_1w'])} this week ({abs(m['z_w']):.1f}σ)"
            return f"{m['name']} {_mv(m)} ({abs(m['z']):.1f}σ)"
        parts.append("out of the ordinary: " + ", ".join(why(m) for m in movers[:3]))
    if parts:
        s = "; ".join(parts) + "."
        out.append(s[0].upper() + s[1:])

    # 5. The main story and what's next
    up = [e for e in events if dt.datetime.fromisoformat(e["time"]) > now and e["kind"] != "minutes"
          and not e.get("minor")]
    s = f"Top story: {news[0]['title'].rstrip('.')} ({news[0]['source']})." if news else ""
    if up:
        e = up[0]
        t = dt.datetime.fromisoformat(e["time"]).astimezone(DUBLIN)
        s += f" Next up: {e['label']} {t:%a %H:%M}"
        s += f" (consensus {e['forecast_str']})." if e.get("forecast") is not None else "."
    if s:
        out.append(s.strip())
    return out or ["Market data is unavailable for this build."]


def claude_summary(cfg: dict, series: dict, movers: list[dict], events: list[dict], news: list[dict],
                   rates: dict | None = None) -> list[str] | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; using rule-based summary")
        return None

    key = {sid for ids in cfg.get("key_markets", {}).values() for sid in ids}

    def brief(v):
        when = (f"live {dt.datetime.fromisoformat(v['at']).astimezone(DUBLIN):%H:%M} Dublin"
                if v.get("basis") == "live" else f"close of {v['date']}")
        return {"name": v["name"], "level": round(v["value"], 4), "as_of": when,
                "move_vs_normal_sigma": None if v.get("z") is None else round(v["z"], 1),
                "chg_1d": None if v.get("chg_1d") is None else round(v["chg_1d"], 2),
                "chg_1w": None if v.get("chg_1w") is None else round(v["chg_1w"], 2),
                "week_move_vs_normal_sigma": None if v.get("z_w") is None else round(v["z_w"], 1),
                "unit_of_change": "bp" if v["is_yield"] else "%", "stale": v.get("stale"),
                "headline_market": v["id"] in key}

    now = now_utc()
    facts = {
        "markets": [brief(v) for k, v in series.items() if not k.startswith("_") and v.get("available")],
        "unusual_moves": [{"name": m["name"], "why": m.get("mover_reasons")} for m in movers],
        "policy_rate_changes_last_3_days": [
            {"bank": b["name"], "new_rate": b["rate"], "change_bp": b["last_change_bp"], "date": b["last_change"]}
            for b in (rates or {}).get("banks", []) if b.get("recent_change")],
        "recent_releases": [{"what": e["label"], "actual": e["actual_str"],
                             "consensus": e["forecast_str"], "previous": e["previous_str"]}
                            for e in events if dt.datetime.fromisoformat(e["time"]) <= now and e.get("actual") is not None][-8:],
        "upcoming": [{"what": e["label"],
                      "when_dublin": dt.datetime.fromisoformat(e["time"]).astimezone(DUBLIN).strftime("%a %d %b %H:%M"),
                      "consensus": e["forecast_str"]}
                     for e in events if dt.datetime.fromisoformat(e["time"]) > now][:6],
        "headlines": [f"{n['title']} ({n['source']})" for n in news[:12]],
    }
    prompt = (
        "You write the 'What's going on' briefing at the top of a global markets dashboard, read once "
        "or twice a day by an investor in Dublin who wants to know what is happening in markets without "
        "being swamped. Using ONLY the facts in the JSON below, write four or five short plain sentences, "
        "one per line: (1) the overall picture in stocks across regions (headline_market items), today "
        "and over the past week; (2) rates and the dollar: direction of yields and the dollar, quoting "
        "the US 10-year; (3) commodities and risk appetite (oil, gold, VIX); (4) what is driving things: "
        "any policy rate change, data surprise, unusual move (large move_vs_normal_sigma or "
        "week_move_vs_normal_sigma) or major story; (5) the next big event. Describe trends in plain words "
        "(rising, falling, mixed) and back them with numbers quoted exactly as given (round sensibly). "
        "Never invent figures or causes that are not in the data, never link a move to a story unless "
        "the data makes the link obvious, and skip anything marked stale. Return only the sentences, "
        "separated by newlines.\n\n"
        + json.dumps(facts, default=str))
    try:
        client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            log.warning("Claude declined the summary request; using rule-based summary")
            return None
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return lines[:5] if lines else None
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s; using rule-based summary", e.status_code)
    except anthropic.APIConnectionError as e:
        log.warning("Claude API unreachable (%s); using rule-based summary", e)
    return None


def _fingerprint(movers, events, rates) -> str:
    now = now_utc()
    recent = sorted(e["key"] for e in events if e.get("actual") is not None
                    and now - dt.timedelta(hours=24) <= dt.datetime.fromisoformat(e["time"]) <= now)
    return json.dumps({
        "day": now.astimezone(DUBLIN).date().isoformat(),
        "movers": sorted(f"{m['id']}{'+' if (m.get('chg_1d') or 0) >= 0 else '-'}" for m in movers),
        "released": recent,
        "rates": sorted(b["id"] for b in rates.get("banks", []) if b.get("recent_change")),
    }, sort_keys=True)


def summarise(cfg, series, movers, events, news, rates, cache: dict) -> tuple[list[str], str, dict]:
    """Returns (sentences, source note, new cache). Claude is asked again only when
    the set of unusual moves, releases or rate changes has changed, or the last
    summary is more than an hour old, so a 15-minute schedule stays cheap."""
    now = now_utc()
    fp = _fingerprint(movers, events, rates)
    if os.environ.get("ANTHROPIC_API_KEY") and cache.get("fingerprint") == fp and cache.get("sentences") \
            and now - dt.datetime.fromisoformat(cache["at"]) < dt.timedelta(minutes=60):
        at = dt.datetime.fromisoformat(cache["at"]).astimezone(DUBLIN)
        return cache["sentences"], f"Written by Claude ({MODEL}) at {at:%H:%M}", cache
    s = claude_summary(cfg, series, movers, events, news, rates)
    if s:
        return s, f"Written by Claude ({MODEL}) at {now.astimezone(DUBLIN):%H:%M}", \
            {"fingerprint": fp, "at": now.isoformat(), "sentences": s}
    return rule_based(cfg, series, movers, events, news, rates), "Rule-based summary from the numbers on this page", cache
