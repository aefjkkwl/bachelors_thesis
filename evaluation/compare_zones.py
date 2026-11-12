"""
compare_zones.py
================
Selects the region pair and evaluation week (thesis, Section 3.3.1.2).

Compares the hourly carbon intensity of all Electricity Maps zones in a folder
pairwise and ranks the pairs by their suitability for a "stable vs. fluctuating
zone, with frequent crossings" comparison. For each pair it computes the
coefficient of variation (CV) of both zones, the number of crossings over the
year, the week with the most crossings and the amplitude in that week.

Input:
    Hourly Electricity Maps CSV exports (one file per zone, 2025), default
    folder data/carbon/ (see data/README.md). The zone is read from the
    "Zone id" column, so the file name does not matter.

Output (default folder results/compare_zones/):
    zone_stats.csv         key figures per zone, incl. share of estimated data
    pairwise_results.csv   all pairs, sorted by suitability
    amplitude_ranking.csv  suitable pairs sorted by amplitude in their best week
    plots/<a>_vs_<b>.png   best week of the TOP_N_PLOTS best pairs

Usage (from the repository root):
    python evaluation/compare_zones.py [input_dir] [output_dir]

Note:
    Pairs are ranked on the life-cycle carbon intensity (see
    CARBON_COL_CANDIDATES), whereas the evaluation itself uses the direct
    variant (thesis, Section 7.4.1).
"""

import sys
import glob
import os
import itertools
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = str(REPO_ROOT / "data" / "carbon")                # folder with the CSV files
OUTPUT_DIR = str(REPO_ROOT / "results" / "compare_zones")     # target folder for the results

TOP_N_PLOTS = 8               # how many of the best pairs are plotted
MIN_YEARLY_CROSSINGS = 100    # below this, a pair counts as "hardly any overlap"
MIN_BEST_WEEK_CROSSINGS = 6   # below this, the best week is not visually convincing
MIN_CV_GAP_FOR_CONTRAST = 0.15  # from here on, the "stable vs. fluctuating" contrast counts as clear
DOMINANCE_THRESHOLD = 0.97    # if one zone is higher in >= 97 % of the hours: structurally no real crossing
HIGH_CV_WARNING = 0.65        # from this CV on: possible data artefact (e.g. a very small balancing zone
                              # with values often close to zero and sudden peaks) instead of real
                              # fluctuation; the result is not discarded automatically but flagged
HIGH_ESTIMATED_WARNING = 0.20  # if >= 20 % of the hours of the recommended week are estimated instead
                               # of measured, this is flagged as a data-quality note

# Column used for the carbon intensity: the first candidate found in the CSV is used.
# "Life cycle" is used if the export contains it (as in the thesis).
# Put the two "(direct)" entries first to select zones on the direct variant instead.
CARBON_COL_CANDIDATES = [
    "Carbon intensity gCO₂eq/kWh (Life cycle)",
    "Carbon intensity gCO2eq/kWh (Life cycle)",   # in case special characters are encoded differently
    "Carbon intensity gCO₂eq/kWh (direct)",
    "Carbon intensity gCO2eq/kWh (direct)",
]

ESTIMATED_COL_CANDIDATES = ["Data estimated"]

# AWS region per Electricity Maps zone (used in the figure titles)
ZONE_TO_AWS_REGION = {
    "CA-QC": "ca-central-1",
    "DE": "eu-central-1",
    "ES": "eu-south-2",
    "FR": "eu-west-3",
    "GB": "eu-west-2",
    "IE": "eu-west-1",
    "IT-NO": "eu-south-1",
    "SE-SE3": "eu-north-1",
    "US-CAL-CISO": "us-west-1",
    "US-MIDA-PJM": "us-east-1",
    "US-NW-BPAT": "us-west-2",
}

plt.rcParams.update({
    "font.family": "Times New Roman", "font.size": 11,
    "mathtext.fontset": "custom", "mathtext.rm": "Times New Roman",
})


# ---------------------------------------------------------------------
# Loading data
# ---------------------------------------------------------------------
def find_carbon_column(df: pd.DataFrame) -> str:
    for c in CARBON_COL_CANDIDATES:
        if c in df.columns:
            return c
    raise ValueError(
        f"No known carbon intensity column found. "
        f"Available columns: {list(df.columns)}"
    )


def load_zone_series(filepath: str) -> tuple[str, pd.DataFrame]:
    """
    Reads a zone CSV and returns (zone, df), where df has two columns:
      - "value":     the carbon intensity (float)
      - "estimated": whether this hourly value was estimated instead of
                     measured according to Electricity Maps (bool)
    If the export has no "Data estimated" column, False is assumed
    (no information on estimated data available).
    """
    df = pd.read_csv(filepath)
    col = find_carbon_column(df)

    if "Zone id" in df.columns and df["Zone id"].notna().any():
        zone = str(df["Zone id"].iloc[0])
    else:
        # fallback: derive the zone name from the file name
        zone = os.path.splitext(os.path.basename(filepath))[0]

    df["dt"] = pd.to_datetime(df["Datetime (UTC)"])
    df = df.set_index("dt").sort_index()

    out = pd.DataFrame(index=df.index)
    out["value"] = df[col]

    estimated_col = next((c for c in ESTIMATED_COL_CANDIDATES if c in df.columns), None)
    if estimated_col is not None:
        out["estimated"] = (
            df[estimated_col].astype(str).str.strip().str.lower().isin(["true", "1", "yes"])
        )
    else:
        out["estimated"] = False

    return zone, out


def load_all_zones(input_dir: str) -> dict[str, pd.DataFrame]:
    files = sorted(glob.glob(os.path.join(input_dir, "*.csv")))
    if not files:
        raise SystemExit(f"No CSV files found in '{input_dir}'.")

    data = {}
    skipped = []
    for f in files:
        try:
            zone, df = load_zone_series(f)
        except (ValueError, KeyError) as e:
            skipped.append((f, str(e)))
            continue
        if zone in data:
            print(f"Warning: zone '{zone}' occurs more than once, overwriting with {f}")
        data[zone] = df

    if skipped:
        print(f"{len(skipped)} file(s) skipped (no matching carbon intensity format):")
        for f, reason in skipped:
            print(f"  - {f}")
        print()

    if not data:
        raise SystemExit(
            "Not a single file could be read as an Electricity Maps export. "
            "Please check that the folder contains only the matching CSV exports."
        )
    return data


# ---------------------------------------------------------------------
# Key figures
# ---------------------------------------------------------------------
def zone_stats(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for zone, df in data.items():
        s = df["value"].dropna()
        mean = s.mean()
        std = s.std()
        pct_estimated = float(df["estimated"].mean() * 100) if len(df) else np.nan
        rows.append(dict(
            zone=zone,
            n_hours=len(s),
            mean=mean,
            std=std,
            cv=std / mean if mean else np.nan,
            min=s.min(),
            max=s.max(),
            pct_estimated=pct_estimated,
        ))
    return pd.DataFrame(rows).set_index("zone").sort_values("cv")


def weekly_crossings(merged: pd.DataFrame) -> pd.Series:
    """
    Counts crossings per calendar week.

    Sign changes are first determined globally (over the whole period at
    once) and only then assigned to the week of the first timestamp
    involved. Computing them per week would lose crossings at week
    boundaries, since np.diff never reaches across the edge of a group.
    """
    sign = np.sign(merged["diff"].values)
    if len(sign) < 2:
        return pd.Series(dtype=int)

    change_mask = np.diff(sign) != 0
    if not change_mask.any():
        return pd.Series(dtype=int)

    # each crossing is assigned to the week of the first of the two timestamps involved
    change_index = merged.index[:-1][change_mask]
    iso = change_index.isocalendar()
    grouping = pd.DataFrame({"year": iso.year.values, "week": iso.week.values})
    return grouping.groupby(["year", "week"]).size()


def evaluate_pair(a: str, b: str, dfa: pd.DataFrame, dfb: pd.DataFrame) -> dict:
    sa, sb = dfa["value"], dfb["value"]
    merged = pd.concat([sa, sb], axis=1, join="inner")
    merged.columns = ["a", "b"]
    merged = merged.dropna()

    if merged.empty:
        return dict(zone_a=a, zone_b=b, overlap_hours=0, verdict="no common timestamps")

    diff = merged["a"] - merged["b"]
    merged["diff"] = diff
    sign = np.sign(diff)

    total_crossings = int((np.diff(sign.values) != 0).sum())
    pct_a_higher = float((sign > 0).mean() * 100)
    dominance = max(pct_a_higher, 100 - pct_a_higher) / 100.0

    wc = weekly_crossings(merged)
    if wc.empty:
        best_year, best_week, best_week_crossings = None, None, 0
    else:
        best_year, best_week = wc.idxmax()
        best_week_crossings = int(wc.max())

    cv_a = sa.std() / sa.mean()
    cv_b = sb.std() / sb.mean()
    cv_gap = abs(cv_a - cv_b)
    possible_artifact = (cv_a >= HIGH_CV_WARNING) or (cv_b >= HIGH_CV_WARNING)

    best_week_pct_estimated = None
    high_estimated_share = False
    if best_year is not None:
        iso_a = dfa.index.isocalendar()
        week_mask_a = (iso_a.year == best_year) & (iso_a.week == best_week)
        iso_b = dfb.index.isocalendar()
        week_mask_b = (iso_b.year == best_year) & (iso_b.week == best_week)

        pct_est_a = float(dfa.loc[week_mask_a, "estimated"].mean() * 100) if week_mask_a.any() else np.nan
        pct_est_b = float(dfb.loc[week_mask_b, "estimated"].mean() * 100) if week_mask_b.any() else np.nan
        best_week_pct_estimated = round(np.nanmax([pct_est_a, pct_est_b]), 1)
        high_estimated_share = (best_week_pct_estimated or 0) / 100.0 >= HIGH_ESTIMATED_WARNING

    # --- amplitude in the best week ---
    range_a = range_b = range_joint = mean_abs_diff = None
    days_with_crossing = longest_gap_h = None
    if best_year is not None:
        iso_m = merged.index.isocalendar()
        wmask = ((iso_m.year == best_year) & (iso_m.week == best_week)).values
        wk = merged.loc[wmask]
        if not wk.empty:
            range_a = float(wk["a"].max() - wk["a"].min())
            range_b = float(wk["b"].max() - wk["b"].min())
            range_joint = float(max(wk["a"].max(), wk["b"].max()) - min(wk["a"].min(), wk["b"].min()))
            mean_abs_diff = float((wk["a"] - wk["b"]).abs().mean())

            # distribution of the crossings within the week
            sgn = np.sign((wk["a"] - wk["b"]).values)
            chg = np.where(np.diff(sgn) != 0)[0]
            cross_times = wk.index[chg + 1] if len(chg) else wk.index[:0]
            days_with_crossing = int(len(set(cross_times.normalize())))
            marks = [wk.index[0]] + list(cross_times) + [wk.index[-1]]
            gaps = [(marks[i + 1] - marks[i]).total_seconds() / 3600 for i in range(len(marks) - 1)]
            longest_gap_h = float(max(gaps)) if gaps else None

    # --- suitability rating ---
    if dominance >= DOMINANCE_THRESHOLD:
        verdict = "unsuitable: one zone dominates almost throughout, no real crossing"
        suitable = False
    elif total_crossings < MIN_YEARLY_CROSSINGS or best_week_crossings < MIN_BEST_WEEK_CROSSINGS:
        verdict = "unsuitable: too few crossings for a clear picture"
        suitable = False
    elif cv_gap < MIN_CV_GAP_FOR_CONTRAST:
        verdict = "partly suitable: many crossings, but little contrast (both similarly volatile)"
        suitable = True
    else:
        verdict = "suitable: clear contrast (stable vs. fluctuating) AND enough crossings"
        suitable = True

    if possible_artifact and suitable:
        verdict += " -- CAUTION: one of the zones has a very high CV (>= " \
                   f"{HIGH_CV_WARNING}), which may also be a data artefact " \
                   "(e.g. a very small balancing zone with values close to zero " \
                   "and occasional peaks instead of real fluctuation) - check the " \
                   "curve before using it"

    if high_estimated_share and suitable:
        verdict += f" -- NOTE: {best_week_pct_estimated} % of the hours in the " \
                   "recommended week are estimated, not measured, according to " \
                   "Electricity Maps - check the data quality of the week before using it"

    return dict(
        zone_a=a, zone_b=b,
        cv_a=round(cv_a, 3), cv_b=round(cv_b, 3), cv_gap=round(cv_gap, 3),
        overlap_hours=len(merged),
        total_crossings=total_crossings,
        pct_a_higher=round(pct_a_higher, 1),
        best_week=f"{best_year}-W{best_week:02d}" if best_year is not None else None,
        best_week_crossings=best_week_crossings,
        best_week_pct_estimated=best_week_pct_estimated,
        best_week_range_a=None if range_a is None else round(range_a, 1),
        best_week_range_b=None if range_b is None else round(range_b, 1),
        best_week_range_joint=None if range_joint is None else round(range_joint, 1),
        best_week_mean_abs_diff=None if mean_abs_diff is None else round(mean_abs_diff, 1),
        best_week_days_with_crossing=days_with_crossing,
        best_week_longest_gap_h=None if longest_gap_h is None else round(longest_gap_h, 1),
        suitable=suitable,
        possible_artifact=possible_artifact,
        high_estimated_share=high_estimated_share,
        verdict=verdict,
    )


def all_pairs(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    zones = list(data.keys())
    rows = [evaluate_pair(a, b, data[a], data[b]) for a, b in itertools.combinations(zones, 2)]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["sort_key"] = (
        df["suitable"].astype(int) * 1000
        + (~df["possible_artifact"]).astype(int) * 500
        + df.get("cv_gap", 0).fillna(0) * 100
        + df.get("total_crossings", 0).fillna(0) / 1_000_000
    )
    df = df.sort_values("sort_key", ascending=False).drop(columns="sort_key")
    return df


# ---------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------
def week_bounds(series_index: pd.DatetimeIndex, year: int, week: int) -> tuple:
    iso = series_index.isocalendar()
    mask = (iso.year == year) & (iso.week == week)
    idx = series_index[mask]
    return idx.min(), idx.max()


def zone_label(zone: str) -> str:
    """'DE (eu-central-1)', or just the zone if no AWS region is known."""
    region = ZONE_TO_AWS_REGION.get(zone)
    return f"{zone} ({region})" if region else zone


def plot_pair(a: str, b: str, dfa: pd.DataFrame, dfb: pd.DataFrame, best_week: str, out_dir: str):
    year_str, week_str = best_week.split("-W")
    year, week = int(year_str), int(week_str)
    start, end = week_bounds(dfa.index, year, week)

    sa_w = dfa.loc[start:end, "value"]
    sb_w = dfb.loc[start:end, "value"]

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(sa_w.index, sa_w.values, label=zone_label(a), color="#1f4e8c", linewidth=1.5)
    ax.plot(sb_w.index, sb_w.values, label=zone_label(b), color="#a61c1c", linewidth=1.5)
    ax.set_ylabel("Carbon intensity [gCO$_2$eq/kWh]")
    ax.set_xlabel("Date (UTC)")
    ax.set_title(f"{zone_label(a)} and {zone_label(b)}, {start.date()} to {end.date()}")
    ax.legend(frameon=False)
    ax.grid(alpha=0.3)
    ax.margins(x=0)
    fig.autofmt_xdate()
    plt.tight_layout()

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{a}_vs_{b}.png".replace("/", "-"))
    plt.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
def main():
    input_dir = sys.argv[1] if len(sys.argv) > 1 else INPUT_DIR
    output_dir = sys.argv[2] if len(sys.argv) > 2 else OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)
    plots_dir = os.path.join(output_dir, "plots")

    print(f"Loading CSV files from: {input_dir}")
    data = load_all_zones(input_dir)
    print(f"{len(data)} zones loaded: {sorted(data.keys())}\n")

    stats = zone_stats(data)
    stats_path = os.path.join(output_dir, "zone_stats.csv")
    stats.to_csv(stats_path)
    print("=== Key figures per zone (sorted by CV) ===")
    print(stats.round(2).to_string())
    print(f"\n-> saved: {stats_path}\n")

    pairs = all_pairs(data)
    pairs_path = os.path.join(output_dir, "pairwise_results.csv")
    pairs.to_csv(pairs_path, index=False)

    print("=== Best pairs (sorted: suitable first, then contrast) ===")
    display_cols = ["zone_a", "zone_b", "cv_a", "cv_b", "cv_gap",
                    "total_crossings", "best_week", "best_week_crossings",
                    "best_week_pct_estimated", "best_week_range_joint",
                    "best_week_mean_abs_diff", "best_week_days_with_crossing",
                    "best_week_longest_gap_h", "suitable", "verdict"]
    print(pairs[display_cols].head(20).to_string(index=False))
    print(f"\n-> full table saved: {pairs_path}\n")

    amp_cols = ["zone_a", "zone_b", "best_week", "best_week_crossings",
                "best_week_range_a", "best_week_range_b", "best_week_range_joint",
                "best_week_mean_abs_diff", "best_week_days_with_crossing",
                "best_week_longest_gap_h"]
    amp = (pairs[pairs["suitable"]]
           .sort_values("best_week_mean_abs_diff", ascending=False)[amp_cols].head(10))
    print("=== Suitable pairs by amplitude (mean |a-b| in the best week) ===")
    print(amp.to_string(index=False))
    amp_path = os.path.join(output_dir, "amplitude_ranking.csv")
    amp.to_csv(amp_path, index=False)
    print(f"\n-> saved: {amp_path}\n")

    suitable_pairs = pairs[pairs["suitable"]].head(TOP_N_PLOTS)
    if suitable_pairs.empty:
        print("No pair meets the suitability criteria for a figure. "
              "Consider relaxing the thresholds at the top of the script, or load more zones.")
        return

    print(f"Creating figures for the {len(suitable_pairs)} best pairs ...")
    for _, row in suitable_pairs.iterrows():
        a, b = row["zone_a"], row["zone_b"]
        path = plot_pair(a, b, data[a], data[b], row["best_week"], plots_dir)
        print(f"  {a} vs {b} -> {path}")

    print("\nDone.")


if __name__ == "__main__":
    main()