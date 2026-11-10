"""Percentile ranks among industry peers, shared by the label and the screens."""

from __future__ import annotations

import numpy as np
import pandas as pd


def peer_pct(df: pd.DataFrame, values: pd.Series, lower_is_cheaper: bool,
             min_peers: int) -> tuple[pd.Series, pd.Series]:
    """Percentile (0 = cheapest) within industry, else sector, else market; plus the level."""
    v = values.where(np.isfinite(values))
    cheap_first = v if lower_is_cheaper else -v
    pct = pd.Series(np.nan, index=df.index)
    level = pd.Series(None, index=df.index, dtype=object)
    for col, name in (("industry_group", "industry"), ("sector", "sector"), (None, "market")):
        todo = v.notna() & pct.isna()
        if not todo.any():
            break
        keys = df[col].fillna("?") if col else pd.Series("all", index=df.index)
        counts = v.notna().groupby(keys).transform("sum")
        ok = todo & ((counts >= min_peers) | (col is None))
        if not ok.any():
            continue
        ranked = cheap_first.where(v.notna()).groupby(keys).rank(pct=True, method="average")
        # rank(pct) is in (0, 1]; shift so the cheapest peer is ~0 and the dearest 1.
        n = counts.astype(float)
        scaled = ((ranked * n) - 1) / (n - 1).where(n > 1, 1)
        pct[ok], level[ok] = scaled[ok], name
    return pct, level
