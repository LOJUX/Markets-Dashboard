"""Render the single self-contained index.html."""
from __future__ import annotations

import datetime as dt
import json
import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .util import DUBLIN, now_utc

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def fmt_level(it: dict) -> str:
    v = it.get("value")
    if v is None:
        return "—"
    if it.get("spread"):
        return f"{v:+.0f}bp"
    if it["is_yield"]:
        return f"{v:.3f}%"
    if it["group"] == "fx" and it["id"] != "DXY":
        return f"{v:.2f}" if v >= 20 else f"{v:.4f}"
    if v >= 10000:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def _num(v):
    return v if isinstance(v, (int, float)) else None


def fmt_chg(v, is_yield: bool) -> str:
    v = _num(v)
    if v is None:
        return "n/a"
    if is_yield:
        return f"{v:+.1f}bp"
    return f"{v:+.2f}%"


def chg_class(v, is_yield: bool) -> str:
    v = _num(v)
    if v is None:
        return "na"
    if is_yield:
        return "neutral"
    if abs(v) < 0.005:
        return "flat"
    return "up" if v > 0 else "down"


def arrow(v) -> str:
    v = _num(v)
    if v is None or abs(v) < 0.005:
        return ""
    return "▲" if v > 0 else "▼"


def sparkline(values: list[float], w: int = 96, h: int = 26) -> str:
    if not values or len(values) < 2:
        return '<span class="muted small">n/a</span>'
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(values):
        x = 2 + i * (w - 6) / (len(values) - 1)
        y = 3 + (h - 6) * (1 - (v - lo) / rng)
        pts.append(f"{x:.1f},{y:.1f}")
    lx, ly = pts[-1].split(",")
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{lx}" cy="{ly}" r="2.2" fill="currentColor"/></svg>')


def dublin(ts: str | None, fmt: str = "%a %d %b %H:%M") -> str:
    if not ts:
        return "—"
    d = dt.datetime.fromisoformat(ts)
    if d.tzinfo is None:
        return d.strftime(fmt)
    return d.astimezone(DUBLIN).strftime(fmt)


def date_fmt(ts: str | None, fmt: str = "%d %b %Y") -> str:
    if not ts:
        return "—"
    return dt.date.fromisoformat(ts[:10]).strftime(fmt)


def when(it: dict) -> str:
    """Short label for what the shown value is: a live time or a close date."""
    if it.get("basis") == "live" and it.get("at"):
        return dublin(it["at"], "%H:%M")
    return "close " + date_fmt(it.get("date"), "%a")


def tip(it: dict) -> str:
    if it.get("spread"):
        return f"{it['source']} · {'live' if it.get('basis') == 'live' else 'close of ' + date_fmt(it['date'], '%a %d %b')}"
    if not it.get("available"):
        return f"Data unavailable. Last attempt {dublin(it.get('fetched_at'))}: {it.get('fetch_error') or 'no data'}"
    parts = [f"Source: {it['source']}"]
    if it.get("basis") == "live":
        parts.append(f"Live quote {dublin(it['at'], '%a %d %b %H:%M')} Dublin")
    else:
        parts.append(f"Close of {date_fmt(it['date'], '%a %d %b %Y')}")
    if it.get("ref_date"):
        parts.append(f"Move measured from the close of {date_fmt(it['ref_date'], '%a %d %b')}")
    if it.get("fetched_at"):
        parts.append(f"Fetched {dublin(it['fetched_at'])} Dublin")
    if not it.get("fetch_ok"):
        parts.append(f"Today's fetch failed ({it.get('fetch_error')}); showing last stored value")
    if it.get("stale"):
        parts.append(f"STALE: expected a close for {date_fmt(it['expected'], '%d %b')}")
    if it.get("sd"):
        parts.append(f"1y daily σ: {it['sd']:.2f}{'bp' if it['is_yield'] else '%'}")
    return " · ".join(parts)


def render(ctx: dict, out_path: str) -> None:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    env.filters.update(level=fmt_level, chg=fmt_chg, chgcls=chg_class, arrow=arrow, dublin=dublin,
                       datefmt=date_fmt, tip=tip, when=when, tojson_compact=lambda o: json.dumps(o, separators=(",", ":"), default=str))
    env.globals.update(sparkline=sparkline)
    html = env.get_template("index.html.j2").render(**ctx, now=now_utc())
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
