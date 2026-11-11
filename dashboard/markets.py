"""Market data fetchers (Yahoo, CNBC, FRED, BoE, Bundesbank, MoF) and history update."""
from __future__ import annotations

import datetime as dt
import io
import logging
import os
import warnings

import pandas as pd

from .store import History
from .util import Status, get, last_completed_session, now_utc

log = logging.getLogger("dashboard")

SOURCE_LABELS = {
    "yahoo": "Yahoo Finance",
    "cnbc": "CNBC",
    "fred": "FRED (St. Louis Fed)",
    "boe": "Bank of England",
    "bundesbank": "Deutsche Bundesbank",
    "mof": "Japan Ministry of Finance",
}


def source_label(src: dict) -> str:
    return f"{SOURCE_LABELS.get(src['type'], src['type'])} ({src['symbol']})"


# ---------------------------------------------------------------- fetchers
class Fetcher:
    """Fetches daily history for one source spec. Yahoo symbols are batched."""

    def __init__(self, yahoo_symbols: list[str], yahoo_period: str):
        self._yahoo_symbols = sorted(set(yahoo_symbols))
        self._yahoo_period = yahoo_period
        self._ycache: dict[str, pd.Series] | None = None
        self._yahoo_error: str | None = None

    def fetch(self, src: dict, days: int) -> pd.Series:
        kind = src["type"]
        fn = getattr(self, f"_{kind}", None)
        if fn is None:
            raise ValueError(f"unknown source type {kind}")
        s = fn(src["symbol"], days)
        s = s.dropna()
        s = s[s.index >= (dt.date.today() - dt.timedelta(days=days))]
        if s.empty:
            raise ValueError("no data returned")
        return s

    # Yahoo ------------------------------------------------------------
    def _load_yahoo(self) -> None:
        import yfinance as yf
        self._ycache = {}
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                df = yf.download(self._yahoo_symbols, period=self._yahoo_period, interval="1d",
                                 progress=False, group_by="ticker", auto_adjust=False,
                                 threads=True, timeout=20)
        except Exception as e:  # whole batch failed
            self._yahoo_error = str(e)
            return
        for sym in self._yahoo_symbols:
            try:
                c = df[sym]["Close"] if len(self._yahoo_symbols) > 1 else df["Close"]
                c = c.dropna()
                c.index = [ts.date() for ts in pd.to_datetime(c.index)]
                self._ycache[sym] = c.astype(float)
            except Exception:
                pass
        # Yahoo's batch endpoint sometimes drops a symbol, or returns it without
        # its latest sessions. Retry those one at a time before giving up.
        newest = max((c.index[-1] for c in self._ycache.values() if len(c)), default=None)
        for sym in self._yahoo_symbols:
            c = self._ycache.get(sym)
            if c is not None and len(c) and newest and (newest - c.index[-1]).days <= 3:
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    one = yf.download(sym, period=self._yahoo_period, interval="1d", progress=False,
                                      auto_adjust=False, threads=False, timeout=20)
                c1 = one["Close"]
                c1 = (c1[sym] if isinstance(c1, pd.DataFrame) else c1).dropna()
                c1.index = [ts.date() for ts in pd.to_datetime(c1.index)]
                if len(c1) and (c is None or not len(c) or c1.index[-1] > c.index[-1]):
                    self._ycache[sym] = c1.astype(float)
                    log.info("Yahoo %s: refetched on its own (batch was missing or behind)", sym)
            except Exception as e:
                log.info("Yahoo %s: single retry failed (%s)", sym, e)

    def _yahoo(self, symbol: str, days: int) -> pd.Series:
        if self._ycache is None:
            self._load_yahoo()
        if symbol not in self._ycache:
            raise ValueError(self._yahoo_error or f"Yahoo returned no data for {symbol}")
        return self._ycache[symbol]

    # CNBC -------------------------------------------------------------
    def _cnbc(self, symbol: str, days: int) -> pd.Series:
        start = (dt.date.today() - dt.timedelta(days=days)).strftime("%Y%m%d000000")
        end = (dt.date.today() + dt.timedelta(days=1)).strftime("%Y%m%d000000")
        j = get(f"https://ts-api.cnbc.com/harmony/app/bars/{symbol}/1D/{start}/{end}/adjusted/EST5EDT.json").json()
        bars = (j.get("barData") or {}).get("priceBars") or []
        out = {}
        for b in bars:
            d = dt.datetime.strptime(b["tradeTime"][:8], "%Y%m%d").date()
            if d.weekday() < 5:
                out[d] = float(b["close"])
        return pd.Series(out, dtype=float).sort_index()

    # FRED -------------------------------------------------------------
    def _fred(self, symbol: str, days: int) -> pd.Series:
        return fred_series(symbol, days)

    # Bank of England --------------------------------------------------
    def _boe(self, symbol: str, days: int) -> pd.Series:
        return boe_series(symbol, dt.date.today() - dt.timedelta(days=days))

    # Bundesbank -------------------------------------------------------
    def _bundesbank(self, symbol: str, days: int) -> pd.Series:
        flow, key = symbol.split("/", 1)
        r = get(f"https://api.statistiken.bundesbank.de/rest/data/{flow}/{key}",
                params={"format": "csv", "lang": "en",
                        "startPeriod": (dt.date.today() - dt.timedelta(days=days)).isoformat()})
        out = {}
        for line in r.content.decode("utf-8-sig").splitlines():
            parts = [p.strip('"') for p in line.split(";" if ";" in line else ",")]
            if len(parts) >= 2 and len(parts[0]) == 10 and parts[0][4] == "-":
                v = parts[1].replace(",", ".")
                if v and v not in (".", "-"):
                    out[dt.date.fromisoformat(parts[0])] = float(v)
        return pd.Series(out, dtype=float).sort_index()

    # Japan MoF --------------------------------------------------------
    def _mof(self, symbol: str, days: int) -> pd.Series:
        url = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
               + ("jgbcme.csv" if days <= 31 else "historical/jgbcme_all.csv"))
        text = get(url).content.decode("utf-8", errors="replace")
        lines = text.splitlines()
        hdr = next(i for i, l in enumerate(lines) if l.startswith("Date,"))
        df = pd.read_csv(io.StringIO("\n".join(lines[hdr:])))
        df = df[df["Date"].astype(str).str.match(r"^\d{4}/\d{1,2}/\d{1,2}$")]
        df["Date"] = pd.to_datetime(df["Date"], format="%Y/%m/%d").dt.date
        vals = pd.to_numeric(df[symbol], errors="coerce")
        return pd.Series(vals.values, index=df["Date"].values, dtype=float).sort_index()



def fred_series(series_id: str, days: int) -> pd.Series:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        raise ValueError("FRED_API_KEY not set")
    j = get("https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": key, "file_type": "json",
                    "observation_start": (dt.date.today() - dt.timedelta(days=days)).isoformat()},
            timeout=45).json()
    out = {dt.date.fromisoformat(o["date"]): float(o["value"])
           for o in j.get("observations", []) if o["value"] not in (".", "")}
    return pd.Series(out, dtype=float).sort_index()


def boe_series(code: str, start: dt.date) -> pd.Series:
    r = get("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp",
            params={"csv.x": "yes", "Datefrom": start.strftime("%d/%b/%Y"), "Dateto": "now",
                    "SeriesCodes": code, "CSVF": "TN", "UsingCodes": "Y", "VPD": "Y", "VFD": "N"})
    df = pd.read_csv(io.StringIO(r.text))
    if code not in df.columns:
        raise ValueError(f"BoE response missing {code}")
    idx = pd.to_datetime(df["DATE"], format="%d %b %Y").dt.date
    return pd.Series(pd.to_numeric(df[code], errors="coerce").values, index=idx.values,
                     dtype=float).sort_index()


# ---------------------------------------------------------------- history update
def _trim_partial(s: pd.Series, spec: dict) -> pd.Series:
    """Drop bars for sessions that haven't closed yet (no partial days in history)."""
    last = last_completed_session(spec["close"])
    if spec.get("weekends"):
        # 24/7 markets: last completed calendar day per the close spec
        from .util import parse_local_time
        t, tz = parse_local_time(spec["close"])
        local = now_utc().astimezone(tz)
        last = local.date() - (dt.timedelta(days=1) if local.time() < t else dt.timedelta(0))
    return s[[d <= last for d in s.index]]


def update_history(cfg: dict, hist: History, status: Status) -> None:
    hcfg = cfg["history"]
    need_backfill = any(hist.count(s["id"]) < hcfg["min_points_for_incremental"]
                        for s in cfg["series"])
    period = "2y" if need_backfill else hcfg["incremental_period"]
    yahoo_syms = [src["symbol"] for s in cfg["series"] for src in s["sources"]
                  if src["type"] == "yahoo"]
    fetcher = Fetcher(yahoo_syms, period)
    for spec in cfg["series"]:
        sid = spec["id"]
        backfill = hist.count(sid) < hcfg["min_points_for_incremental"]
        days = hcfg["backfill_days"] if backfill else 35
        for src in spec["sources"]:
            label = source_label(src)
            try:
                s = _trim_partial(fetcher.fetch(src, days), spec)
                if s.empty:
                    raise ValueError("only an unfinished session was returned")
                n = hist.upsert(sid, s, label)
                status.ok(sid, label, f"{n} rows, last {s.index[-1]}")
                log.info("%-7s %-40s %4d rows (last %s)", sid, label, n, s.index[-1])
                break
            except Exception as e:
                status.fail(sid, label, e)


def run_cross_checks(cfg: dict, hist: History) -> list[dict]:
    """Compare displayed yields against independent official sources."""
    results = []
    fetcher = Fetcher([], "1mo")
    for chk in cfg.get("cross_checks", []):
        sid, src = chk["series"], chk["source"]
        row = {"series": sid, "source": source_label(src), "note": chk.get("note", ""),
               "tolerance_bp": chk["tolerance_bp"]}
        try:
            other = fetcher.fetch(src, 20)
            mine = hist.series(sid)
            mine = dict(zip(mine["date"], mine["value"]))
            common = [d for d in other.index if d in mine]
            if not common:
                raise ValueError("no overlapping dates")
            d = max(common)
            diff_bp = (mine[d] - float(other[d])) * 100
            row.update(date=d.isoformat(), ours=mine[d], theirs=float(other[d]),
                       diff_bp=round(diff_bp, 1), ok=abs(diff_bp) <= chk["tolerance_bp"])
        except Exception as e:
            row.update(ok=None, error=str(e)[:200])
        results.append(row)
    return results


# ---------------------------------------------------------------- live quotes
MAX_QUOTE_AGE = dt.timedelta(days=4)


def _yahoo_live(symbols: list[str]) -> dict[str, tuple[float, dt.datetime]]:
    """Latest 5-minute bar for each symbol, in one batched request."""
    import yfinance as yf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = yf.download(symbols, period="5d", interval="5m", progress=False,
                         auto_adjust=False, group_by="column", threads=True, timeout=20)
    out = {}
    if df is None or df.empty:
        return out
    close = df["Close"]
    if not hasattr(close, "columns"):
        close = close.to_frame(symbols[0])
    for sym in close.columns:
        c = close[sym].dropna()
        if c.empty:
            continue
        ts = pd.Timestamp(c.index[-1])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        out[str(sym)] = (float(c.iloc[-1]), ts.to_pydatetime())
    return out


def _cnbc_live(symbols: list[str]) -> dict[str, tuple[float, dt.datetime]]:
    j = get("https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol",
            params={"symbols": "|".join(symbols), "requestMethod": "itv", "noform": "1",
                    "partnerId": "2", "fund": "1", "exthrs": "1", "output": "json"}, timeout=15).json()
    quotes = j["FormattedQuoteResult"]["FormattedQuote"]
    if isinstance(quotes, dict):
        quotes = [quotes]
    out = {}
    for q in quotes:
        try:
            v = float(str(q["last"]).replace("%", "").replace(",", ""))
            ts = dt.datetime.strptime(q["last_time"], "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(dt.timezone.utc)
            out[q["symbol"]] = (v, ts)
        except (KeyError, ValueError):
            continue
    return out


def live_quotes(cfg: dict, hist: History, status: Status) -> dict[str, dict]:
    """Latest intraday quote for every series, from the same source as its stored
    history (so 'since last close' never mixes sources). Returns
    {series_id: {"value", "at" (UTC datetime), "source"}}."""
    wanted: dict[str, dict] = {}
    for spec in cfg["series"]:
        rows = hist.series(spec["id"])
        if rows.empty:
            continue
        latest = rows["source"].iloc[-1]
        for src in spec["sources"]:
            if src["type"] in ("yahoo", "cnbc") and source_label(src) == latest:
                wanted[spec["id"]] = src
                break

    got: dict[str, dict[str, tuple]] = {}
    for kind, fn in (("yahoo", _yahoo_live), ("cnbc", _cnbc_live)):
        syms = sorted({s["symbol"] for s in wanted.values() if s["type"] == kind})
        if not syms:
            continue
        try:
            got[kind] = fn(syms)
            status.ok(f"live_{kind}", f"{SOURCE_LABELS[kind]} intraday", f"{len(got[kind])}/{len(syms)} quotes")
        except Exception as e:
            got[kind] = {}
            status.fail(f"live_{kind}", f"{SOURCE_LABELS[kind]} intraday", e)

    now = now_utc()
    out = {}
    for sid, src in wanted.items():
        q = got.get(src["type"], {}).get(src["symbol"])
        if q and now - q[1] <= MAX_QUOTE_AGE:
            out[sid] = {"value": q[0], "at": q[1], "source": source_label(src) + " intraday"}
    log.info("live quotes: %d of %d series", len(out), len(wanted))
    return out
