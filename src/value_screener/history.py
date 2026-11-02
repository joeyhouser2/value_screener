"""Phase 9a — fiscal-year history per company from the same slimmed companyfacts.

The TTM screen only needs the latest twelve months; normalized ("through-the-cycle")
metrics, the Piotroski F-score and Altman Z need one value per fiscal year. Each
10-K reports two or three years of comparatives, so XBRL history usually reaches
back to ~2009.

Per concept, tags are merged across time: the tag with the most recent period wins
wherever tags overlap (as in :mod:`fundamentals`), and older tags fill earlier years
(e.g. ``Revenues`` before the ASC 606 tag). Fiscal-year ends come from annual
(350-380 day) income / cash-flow periods; balance-sheet values are matched to them
within ``TOL`` so 52/53-week calendars line up.

Each value keeps the latest filed figure (restatements win) plus ``filed`` and
``first_filed`` so a later backtest can tell what was known when. A few concepts are
derived when the direct tag is missing (``src = "derived"``): gross profit, total
liabilities and total debt.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import fundamentals   # module import: fundamentals imports us back

TOL = pd.Timedelta(days=15)
ANCHORS = ("revenue", "net_income", "operating_income", "operating_cash_flow")
# Inputs to derived concepts; not written out themselves.
HELPERS = {"liabilities_and_equity", "equity_incl_nci", "debt_total", "debt_noncurrent",
           "debt_current", "short_term_borrowings", "notes_payable"}
HISTORY_KINDS = {"flow", "instant", "average"}
COLUMNS = ["cik", "fy_end", "concept", "val", "src", "tag", "filed", "first_filed"]


def _latest_per_end(df: pd.DataFrame) -> pd.DataFrame:
    """One row per period end: latest filed value, its filing date and the first one."""
    if df.empty:
        return pd.DataFrame(columns=["val", "filed", "first_filed"],
                            index=pd.DatetimeIndex([], name="end"))
    df = df.sort_values("filed")
    g = df.groupby("end")
    return pd.DataFrame({"val": g["val"].last(), "filed": g["filed"].last(),
                         "first_filed": g["filed"].min()}).sort_index()


def annual_periods(rows) -> pd.DataFrame:
    """Full-year values (flows or period averages) by period end."""
    df = fundamentals._frame(rows).dropna(subset=["start"])
    df = df[(df["end"] - df["start"]).dt.days.between(350, 380)]
    return _latest_per_end(df)


def instants(rows) -> pd.DataFrame:
    return _latest_per_end(fundamentals._frame(rows))


def concept_series(slim: dict, spec: dict) -> pd.DataFrame:
    """Per-end values for one concept, merged across its tags (newest tag wins overlaps)."""
    reader = instants if spec["kind"] == "instant" else annual_periods
    parts = []
    for prio, tag in enumerate(spec["tags"]):
        rows = slim["tags"].get(tag)
        if rows:
            s = reader(rows)
            if not s.empty:
                parts.append((s.index.max(), -prio, tag, s))
    out = None
    for *_, tag, s in sorted(parts, key=lambda p: (p[0], p[1]), reverse=True):
        s = s.assign(tag=tag)
        if out is None:
            out = s
        else:   # older tags only fill fiscal years the newer ones don't cover
            gap = [e for e in s.index if not (abs(out.index - e) <= TOL).any()]
            out = pd.concat([out, s.loc[gap]]).sort_index()
    return out if out is not None else pd.DataFrame(columns=["val", "filed", "first_filed", "tag"])


def fiscal_year_ends(series: dict[str, pd.DataFrame]) -> list[pd.Timestamp]:
    """Annual period ends of the anchor flows, de-duplicated within ``TOL``."""
    ends = sorted({e for k in ANCHORS if k in series for e in series[k].index})
    out: list[pd.Timestamp] = []
    for e in ends:
        if out and e - out[-1] <= TOL:
            out[-1] = e
        else:
            out.append(e)
    return out


def _align(s: pd.DataFrame, fy_ends: list[pd.Timestamp]) -> pd.DataFrame:
    """Rows of ``s`` re-indexed onto the fiscal-year ends (nearest within ``TOL``)."""
    if s.empty or not fy_ends:
        return pd.DataFrame(columns=["val", "filed", "first_filed", "tag"])
    left = pd.DataFrame({"fy_end": pd.DatetimeIndex(fy_ends)})
    right = s.rename_axis("end").reset_index().sort_values("end")
    right["end"] = pd.to_datetime(right["end"])
    m = pd.merge_asof(left, right, left_on="fy_end", right_on="end",
                      direction="nearest", tolerance=TOL)
    return m.dropna(subset=["val"]).set_index("fy_end").drop(columns="end")


def _derive(a: dict[str, pd.DataFrame], name: str, parts: list[tuple[str, float]],
            fill_zero: tuple[str, ...] = ()) -> None:
    """Fill ``a[name]`` where missing with sum(sign * part); every part must exist
    except those in ``fill_zero`` (taken as 0 when absent)."""
    have = a.get(name, pd.DataFrame(columns=["val"]))
    frames = {k: a.get(k, pd.DataFrame(columns=["val", "filed", "first_filed"])) for k, _ in parts}
    required = [k for k, _ in parts if k not in fill_zero]
    if not required:
        return
    idx = frames[required[0]].index
    for k in required[1:]:
        idx = idx.intersection(frames[k].index)
    idx = idx.difference(have.index)
    if idx.empty:
        return
    val = sum(sign * frames[k]["val"].reindex(idx).astype(float).fillna(0.0) for k, sign in parts)
    present = [frames[k].reindex(idx) for k, _ in parts]
    filed = pd.concat([p["filed"] for p in present], axis=1).max(axis=1)
    first = pd.concat([p["first_filed"] for p in present], axis=1).max(axis=1)
    derived = pd.DataFrame({"val": val, "filed": filed, "first_filed": first,
                            "tag": None, "src": "derived"}, index=idx)
    a[name] = pd.concat([have, derived]).sort_index()


def company_history(slim: dict, tags_cfg: dict, *, max_years: int | None = None) -> pd.DataFrame:
    """Long-format fiscal-year history (fy_end, concept, val, src, tag, filed, first_filed)."""
    series = {k: concept_series(slim, spec) for k, spec in tags_cfg.items()
              if spec["kind"] in HISTORY_KINDS}
    fy_ends = fiscal_year_ends(series)
    if max_years:
        fy_ends = fy_ends[-max_years:]
    aligned = {k: _align(s, fy_ends).assign(src="reported") for k, s in series.items()}

    _derive(aligned, "gross_profit", [("revenue", 1), ("cost_of_revenue", -1)])
    eq_nci = aligned.get("equity_incl_nci")
    if eq_nci is None or eq_nci.empty:
        aligned["equity_incl_nci"] = aligned.get("equity", pd.DataFrame(columns=["val"]))
    _derive(aligned, "liabilities", [("liabilities_and_equity", 1), ("equity_incl_nci", -1)])
    # Total debt: total tag, else noncurrent + current; plus short-term borrowings.
    _derive(aligned, "debt_total", [("debt_noncurrent", 1), ("debt_current", 1)],
            fill_zero=("debt_current",))
    _derive(aligned, "debt_total", [("notes_payable", 1)])
    debt = aligned.get("debt_total", pd.DataFrame(columns=["val"])).copy()
    stb = aligned.get("short_term_borrowings", pd.DataFrame(columns=["val"]))
    if not debt.empty:
        add = stb["val"].reindex(debt.index).astype(float).fillna(0.0)
        debt["val"] = debt["val"].astype(float) + add
        debt.loc[add != 0, "src"] = "derived"
    aligned["total_debt"] = debt

    frames = []
    for concept, df in aligned.items():
        if concept in HELPERS or df.empty:
            continue
        frames.append(df.rename_axis("fy_end").reset_index().assign(concept=concept))
    if not frames:
        return pd.DataFrame(columns=COLUMNS[1:])
    out = pd.concat(frames, ignore_index=True)
    out["val"] = out["val"].astype(float)
    return out[COLUMNS[1:]].sort_values(["fy_end", "concept"]).reset_index(drop=True)


def to_frame(per_cik: dict[int, pd.DataFrame]) -> pd.DataFrame:
    """Stack per-company histories into one long table keyed by CIK."""
    parts = [h.assign(cik=cik) for cik, h in per_cik.items() if h is not None and not h.empty]
    if not parts:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat(parts, ignore_index=True)[COLUMNS]
    df["cik"] = df["cik"].astype("int64")
    for c in ("concept", "src", "tag"):
        df[c] = df[c].astype("category")
    return df


def wide(history_df: pd.DataFrame) -> pd.DataFrame:
    """One row per (cik, fy_end), one column per concept — handy for metric code."""
    return (history_df.pivot_table(index=["cik", "fy_end"], columns="concept", values="val",
                                   aggfunc="last", observed=True)
            .rename_axis(columns=None).reset_index())


def path(settings) -> Path:
    return settings.data_dir / "fundamentals_history.parquet"


def load(settings, cik: int | None = None) -> pd.DataFrame:
    """The whole history, or one company's without reading the rest into memory."""
    return pd.read_parquet(path(settings), filters=None if cik is None else [("cik", "==", int(cik))])
