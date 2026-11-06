"""'Today in 3 sentences': Claude API if ANTHROPIC_API_KEY is set, otherwise rule-based."""
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


def _mv(it):
    return _bp(it["chg_1d"]) if it["is_yield"] else _pct(it["chg_1d"])


def rule_based(series: dict, movers: list[dict], events: list[dict], news: list[dict],
               rates: dict | None = None) -> list[str]:
    ok = {k: v for k, v in series.items() if not k.startswith("_") and v.get("available")
          and v.get("chg_1d") is not None and not v.get("stale")}
    out = []

    if movers:
        bits = [f"{m['name']} {_mv(m)}" + (f" ({abs(m['z']):.1f}σ)" if m.get("z") else "") for m in movers[:3]]
        out.append("Out of the ordinary: " + ", ".join(bits) + ".")
    else:
        eq = [ok[k] for k in ("SPX", "SXXP", "NKY") if k in ok]
        if eq:
            out.append("No unusual moves; " + ", ".join(f"{v['name']} {_pct(v['chg_1d'])}" for v in eq) + ".")

    parts = []
    for b in (rates or {}).get("banks", []):
        if b.get("recent_change") and (now_utc().date() - dt.date.fromisoformat(b["last_change"][:10])).days <= 1:
            verb = "raised" if b["last_change_bp"] > 0 else "cut"
            d = dt.date.fromisoformat(b["last_change"][:10])
            parts.append(f"the {b['short']} {verb} {b['rate_label']} {abs(b['last_change_bp'])}bp "
                         f"to {b['rate']:.2f}% on {d:%a %d %b}")
    now = now_utc()
    surprises = [e for e in events if e.get("surprise") and e["kind"] == "data"
                 and now - dt.timedelta(hours=24) <= dt.datetime.fromisoformat(e["time"]) <= now]
    if surprises:
        e = surprises[-1]
        parts.append(f"{e['label']} came in at {e['actual_str']} against {e['forecast_str']} expected")
    if not parts:
        if "US10Y" in ok:
            parts.append(f"the US 10-year yield is {ok['US10Y']['value']:.2f}% ({_bp(ok['US10Y']['chg_1d'])})")
        if "DE10Y" in ok:
            parts.append(f"the Bund {ok['DE10Y']['value']:.2f}% ({_bp(ok['DE10Y']['chg_1d'])})")
    if parts:
        s = "; ".join(parts) + "."
        out.append(s[0].upper() + s[1:])

    now = now_utc()
    up = [e for e in events if dt.datetime.fromisoformat(e["time"]) > now and e["kind"] != "minutes"]
    if news:
        s = f"Top story: {news[0]['title'].rstrip('.')} ({news[0]['source']})."
    else:
        s = ""
    if up:
        e = up[0]
        t = dt.datetime.fromisoformat(e["time"]).astimezone(DUBLIN)
        s += f" Next up: {e['label']} {t:%a %H:%M}"
        s += f" (consensus {e['forecast_str']})." if e.get("forecast") is not None else "."
    if s:
        out.append(s.strip())
    return out[:3] or ["Market data is unavailable for this build."]


def claude_summary(series: dict, movers: list[dict], events: list[dict], news: list[dict],
                   rates: dict | None = None) -> list[str] | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; using rule-based summary")
        return None

    def brief(v):
        when = (f"live {dt.datetime.fromisoformat(v['at']).astimezone(DUBLIN):%H:%M} Dublin"
                if v.get("basis") == "live" else f"close of {v['date']}")
        return {"name": v["name"], "level": round(v["value"], 4), "as_of": when,
                "move_vs_normal_sigma": None if v.get("z") is None else round(v["z"], 1),
                "chg_1d": None if v.get("chg_1d") is None else round(v["chg_1d"], 2),
                "unit_of_change": "bp" if v["is_yield"] else "%", "stale": v.get("stale")}

    now = now_utc()
    facts = {
        "markets": [brief(v) for k, v in series.items() if not k.startswith("_") and v.get("available")],
        "unusual_moves": [m["name"] for m in movers],
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
        "You write the 'Today in 3 sentences' box at the top of a global markets dashboard that "
        "refreshes every 15 minutes, read by an investor in Dublin. The page focuses on what is "
        "out of the ordinary. Using ONLY the facts in the JSON below, write exactly three plain "
        "sentences: (1) the most unusual market moves (largest |move_vs_normal_sigma|), (2) any "
        "policy rate change or data surprise, otherwise rates/FX/commodities, (3) the most "
        "important story or the next big event. Quote numbers exactly as "
        "given (round sensibly), never invent figures or causes that are not in the data, and skip "
        "anything marked stale. Return the three sentences separated by newlines, nothing else.\n\n"
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
        return lines[:3] if lines else None
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


def summarise(series, movers, events, news, rates, cache: dict) -> tuple[list[str], str, dict]:
    """Returns (sentences, source note, new cache). Claude is asked again only when
    the set of unusual moves, releases or rate changes has changed, or the last
    summary is more than an hour old, so a 15-minute schedule stays cheap."""
    now = now_utc()
    fp = _fingerprint(movers, events, rates)
    if os.environ.get("ANTHROPIC_API_KEY") and cache.get("fingerprint") == fp and cache.get("sentences") \
            and now - dt.datetime.fromisoformat(cache["at"]) < dt.timedelta(minutes=60):
        at = dt.datetime.fromisoformat(cache["at"]).astimezone(DUBLIN)
        return cache["sentences"], f"Written by Claude ({MODEL}) at {at:%H:%M}", cache
    s = claude_summary(series, movers, events, news, rates)
    if s:
        return s, f"Written by Claude ({MODEL}) at {now.astimezone(DUBLIN):%H:%M}", \
            {"fingerprint": fp, "at": now.isoformat(), "sentences": s}
    return rule_based(series, movers, events, news, rates), "Rule-based summary from the numbers on this page", cache
