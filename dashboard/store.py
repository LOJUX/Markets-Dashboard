"""History store: one long-format CSV (date, series, value, source) committed to the repo."""
from __future__ import annotations

import datetime as dt
import os

import pandas as pd

COLUMNS = ["date", "series", "value", "source"]


class History:
    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path) and os.path.getsize(path) > 0:
            df = pd.read_csv(path, dtype={"series": str, "source": str})
            df["date"] = pd.to_datetime(df["date"]).dt.date
        else:
            df = pd.DataFrame(columns=COLUMNS)
        self.df = df

    def count(self, series_id: str) -> int:
        return int((self.df["series"] == series_id).sum())

    def upsert(self, series_id: str, values: pd.Series, source: str) -> int:
        """Insert/replace values (index: date) for a series. Returns rows written."""
        values = values.dropna()
        if values.empty:
            return 0
        new = pd.DataFrame({"date": list(values.index), "series": series_id,
                            "value": values.astype(float).round(6).values, "source": source})
        mask = (self.df["series"] == series_id) & (self.df["date"].isin(set(new["date"])))
        kept = self.df[~mask]
        self.df = pd.concat([kept, new], ignore_index=True) if len(kept) else new
        return len(new)

    def series(self, series_id: str) -> pd.DataFrame:
        """Rows for one series sorted by date: columns date, value, source."""
        d = self.df[self.df["series"] == series_id].sort_values("date")
        return d[["date", "value", "source"]].reset_index(drop=True)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        out = self.df.sort_values(["series", "date"]).copy()
        out["date"] = out["date"].map(lambda d: d.isoformat() if isinstance(d, dt.date) else d)
        out.to_csv(self.path, index=False, columns=COLUMNS)
