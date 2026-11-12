"""
Add transmission carbon to the Baseline and Caribou results (Caribou Eq. 7.5).

Idea
----
evaluate_carbon.py only accounts for execution and keep-alive.
Caribou additionally accounts for the carbon of data transfer:

    transmission [g] = data volume [GB] x energy factor [kWh/GB] x route CI [g/kWh]

This script adds it on top of the existing results without changing evaluate_carbon.py.

Assumptions (as in Caribou's carbon_calculator.py)
--------------------------------------------------
1. Data volume per invocation = start_hop_mb + during_exec_mb from transfer_sizes.csv
   (measured by Caribou via Lambda Insights; averaged over the selected versions).
2. The data (S3 bucket) lives in the home region (--home).
3. Route CI:
      invocation in the home region   -> CI of the home region in that hour
      invocation in the other region  -> mean of home-region CI and target-region CI
4. Two cases as in the Caribou paper (Section 7.1):
      best : 0.001 kWh/GB for every transfer, including intra-region
      worst: 0.005 kWh/GB between regions, 0 within a region
5. Invocations per app and hour come from the same Azure traces as in
   evaluate_carbon.py (same start, same start minute). The loading functions are
   imported directly from evaluate_carbon.py so that everything is identical.

The script must therefore be located in the same folder as evaluate_carbon.py.

Usage (from the repository root; the result in the thesis, Section 7.4.2)
-----------------------------------------------------------------------
python evaluation/transmission_carbon.py \
  --transfer results/transfer_sizes.csv \
  --placements results/caribou_replay_home_us/placements.csv \
  --home us-east-1 \
  --ci eu-central-1=data/carbon/snapshots_2026-02-10_DE-2025-hourly.csv \
  --ci us-east-1=data/carbon/snapshots_2026-02-10_US-MIDA-PJM-2025-hourly.csv \
  --ecolife-dir <ECOLIFE_REPO> \
  --baseline-dir results/baseline_caribou_5minKAT/baseline_us_ecolife \
  --caribou-dir results/baseline_caribou_5minKAT/caribou_home_us_ecolife \
  --kat 10 --pool-rule ecolife \
  --out results/transmission_home_us
"""

import argparse
import csv
import os
from collections import defaultdict
from datetime import datetime, timezone

from evaluate_carbon import APP_TO_WORKFLOW, load_ci, load_placements, load_traces


# --------------------------------------------------------------------------- #
# Step 1: data volume per workflow
# --------------------------------------------------------------------------- #
def load_transfer(path, versions=None):
    """{workflow: MB per invocation}, averaged over the selected versions."""
    per_wf = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wf, version = row["workflow"].rsplit("-", 1)
            if versions and version not in versions:
                continue
            start = float(row["start_hop_mb_mean"] or 0)
            during = float(row["during_exec_mb_mean"] or 0)
            per_wf[wf].append((version, start + during))

    print("\nStep 1: data volume per invocation (MB)")
    result = {}
    for wf, vals in sorted(per_wf.items()):
        mean = sum(v for _, v in vals) / len(vals)
        result[wf] = mean
        detail = ", ".join(f"{ver}: {v:.3f}" for ver, v in vals)
        print(f"  {wf:28s} mean {mean:9.3f}   ({detail})")
    return result


# --------------------------------------------------------------------------- #
# Step 2: invocations per app and hour
# --------------------------------------------------------------------------- #
def calls_per_hour(app_names, traces, start_minute, total_minutes):
    """{app: [invocations in hour 0, 1, ...]} - same minutes as evaluate_carbon.simulate.

    Several trace files can belong to the same app (e.g. bfs-1000k*115.txt, bfs-1000k*7.txt).
    They are summed, exactly as evaluate_carbon.py sums them in per_app.csv.
    """
    hours = total_minutes // 60
    out = {}
    files = defaultdict(int)
    for app, trace in zip(app_names, traces):
        per_h = out.setdefault(app, [0] * hours)
        files[app] += 1
        for t in range(start_minute, total_minutes):
            per_h[t // 60] += trace[t]
    print(f"\nStep 2: {len(traces)} trace files for {len(out)} apps "
          f"({', '.join(f'{a}: {n}' for a, n in sorted(files.items()))}), "
          f"{sum(sum(v) for v in out.values())} invocations")
    return out


# --------------------------------------------------------------------------- #
# Step 3: transmission carbon
# --------------------------------------------------------------------------- #
def transmission(calls, gb_per_call, region_of_hour, home, ci, f_best, f_worst):
    """Returns (invocations, shifted invocations, GB total, GB shifted, g best, g worst)."""
    n = shifted = 0
    gb_all = gb_shift = 0.0
    g_best = g_worst = 0.0
    for h, k in enumerate(calls):
        if k == 0:
            continue
        region = region_of_hour(h)
        gb = k * gb_per_call
        n += k
        gb_all += gb
        if region == home:
            route_ci = ci[home][h]
            g_best += gb * f_best * route_ci
            # worst: intra-region transfer is free
        else:
            route_ci = (ci[home][h] + ci[region][h]) / 2
            shifted += k
            gb_shift += gb
            g_best += gb * f_best * route_ci
            g_worst += gb * f_worst * route_ci
    return n, shifted, gb_all, gb_shift, g_best, g_worst


# --------------------------------------------------------------------------- #
# Step 4: combine with the results of evaluate_carbon.py
# --------------------------------------------------------------------------- #
def read_totals(result_dir, kat, pool_rule):
    """{hardware: total_g} from summary.csv for the selected KAT."""
    totals = {}
    with open(os.path.join(result_dir, "summary.csv"), newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("pool_rule", pool_rule) != pool_rule:
                continue
            try:
                same_kat = float(row["kat_min"]) == float(kat)
            except ValueError:
                same_kat = row["kat_min"] == kat
            if same_kat:
                totals[row["hardware"]] = float(row["total_g"])
    return totals


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transfer", required=True, help="transfer_sizes.csv")
    ap.add_argument("--versions", nargs="*", help="only average these versions, e.g. 3.0.0 3.0.1")
    ap.add_argument("--placements", required=True, help="Caribou's placements.csv (replay)")
    ap.add_argument("--home", required=True, help="home region = location of the data, e.g. us-east-1")
    ap.add_argument("--ci", nargs="+", action="extend", required=True, metavar="REGION=CSV",
                    help="repeat the flag or give several values after one --ci")
    ap.add_argument("--column", default="Carbon intensity gCO₂eq/kWh (direct)")
    ap.add_argument("--ecolife-dir", help="for selected_trace.zip")
    ap.add_argument("--trace-dir", help="folder or selected_trace.zip")
    ap.add_argument("--start", default="2025-08-25T00:00:00")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start-minute", type=int, default=20)
    ap.add_argument("--best", type=float, default=0.001, help="kWh/GB, best case")
    ap.add_argument("--worst", type=float, default=0.005, help="kWh/GB, worst case")
    ap.add_argument("--baseline-dir", help="Baseline result folder (summary.csv), same home region")
    ap.add_argument("--caribou-dir", help="Caribou result folder (summary.csv), same home region")
    ap.add_argument("--kat", default="10")
    ap.add_argument("--pool-rule", default="ecolife")
    ap.add_argument("--out", default="transmission")
    args = ap.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    hours = args.days * 24
    total_minutes = hours * 60

    ci = {}
    for spec in args.ci:
        region, path = spec.split("=", 1)
        ci[region] = load_ci(path, args.column, start, hours)
    if args.home not in ci:
        raise SystemExit(f"--home {args.home} has no CI. Available: {sorted(ci)}")

    mb = load_transfer(args.transfer, args.versions)

    trace_dir = args.trace_dir
    if trace_dir is None:
        if not args.ecolife_dir:
            raise SystemExit("Provide --ecolife-dir or --trace-dir")
        trace_dir = os.path.join(args.ecolife_dir, "selected_trace")
        if not os.path.isdir(trace_dir):
            trace_dir = os.path.join(args.ecolife_dir, "selected_trace.zip")
    if not (os.path.isdir(trace_dir) or os.path.isfile(trace_dir)):
        raise SystemExit(f"Traces not found: {trace_dir}\n"
                         "Check --ecolife-dir (folder containing selected_trace.zip) or pass --trace-dir.")
    app_names, traces = load_traces(trace_dir, total_minutes)
    calls = calls_per_hour(app_names, traces, args.start_minute, total_minutes)

    plan = load_placements(args.placements, start, hours)
    missing = {r for regs in plan.values() for r in regs} - set(ci)
    if missing:
        raise SystemExit(f"No CI for region(s) in placements.csv: {sorted(missing)}")

    print(f"\nSteps 2/3: transmission per app (home region {args.home}, "
          f"best {args.best} kWh/GB, worst {args.worst} kWh/GB)")
    rows = []
    tot = defaultdict(float)
    for app in sorted(calls):
        wf = APP_TO_WORKFLOW[app]
        if wf not in mb:
            raise SystemExit(f"transfer_sizes.csv does not contain {wf}")
        gb_per_call = mb[wf] / 1024

        # Baseline: all invocations in the home region
        b = transmission(calls[app], gb_per_call, lambda h: args.home, args.home, ci, args.best, args.worst)
        # Caribou: region according to the plan
        c = transmission(calls[app], gb_per_call, lambda h, wf=wf: plan[wf][h], args.home, ci,
                         args.best, args.worst)

        row = {
            "app": app, "workflow": wf, "mb_per_call": round(mb[wf], 4),
            "invocations": c[0], "shifted": c[1], "shifted_share": round(c[1] / c[0], 4) if c[0] else 0,
            "gb_total": round(c[2], 2), "gb_shifted": round(c[3], 2),
            "baseline_best_g": round(b[4], 2), "baseline_worst_g": round(b[5], 2),
            "caribou_best_g": round(c[4], 2), "caribou_worst_g": round(c[5], 2),
        }
        rows.append(row)
        for k in ("invocations", "shifted", "gb_total", "gb_shifted",
                  "baseline_best_g", "baseline_worst_g", "caribou_best_g", "caribou_worst_g"):
            tot[k] += row[k]
        print(f"  {app:15s} {mb[wf]:8.3f} MB  invocations {c[0]:9d}  shifted {c[1] / max(c[0], 1):6.1%}  "
              f"Caribou worst {c[5] / 1000:7.3f} kg  best {c[4] / 1000:6.3f} kg  "
              f"(Baseline best {b[4] / 1000:6.3f} kg)")

    os.makedirs(args.out, exist_ok=True)
    out_csv = os.path.join(args.out, "transmission_per_app.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"\nTotal: {tot['invocations']:.0f} invocations, {tot['shifted']:.0f} of them shifted "
          f"({tot['shifted'] / tot['invocations']:.1%})")
    print(f"       {tot['gb_total']:.0f} GB in total, {tot['gb_shifted']:.0f} GB across regions")
    print(f"       Baseline: best {tot['baseline_best_g'] / 1000:.2f} kg, worst {tot['baseline_worst_g'] / 1000:.2f} kg")
    print(f"       Caribou : best {tot['caribou_best_g'] / 1000:.2f} kg, worst {tot['caribou_worst_g'] / 1000:.2f} kg")

    # Step 4
    if args.baseline_dir and args.caribou_dir:
        base = read_totals(args.baseline_dir, args.kat, args.pool_rule)
        car = read_totals(args.caribou_dir, args.kat, args.pool_rule)
        # Cross-check: same number of invocations as in evaluate_carbon.py?
        with open(os.path.join(args.caribou_dir, "summary.csv"), newline="", encoding="utf-8-sig") as f:
            inv = {int(float(r["invocations"])) for r in csv.DictReader(f) if r.get("invocations")}
        if inv and inv != {int(tot["invocations"])}:
            print(f"\nWARNING: summary.csv has {sorted(inv)} invocations, this script counted "
                  f"{int(tot['invocations'])}. Check --ecolife-dir / --trace-dir.")
        else:
            print(f"\nCheck: invocations match summary.csv ({int(tot['invocations'])})")
        print(f"\nStep 4: Caribou savings vs. Baseline (KAT {args.kat}, pool rule {args.pool_rule})")
        summary_rows = []
        for hw in sorted(set(base) & set(car)):
            line = {"hardware": hw}
            for case, b_add, c_add in (("none", 0.0, 0.0),
                                       ("best", tot["baseline_best_g"], tot["caribou_best_g"]),
                                       ("worst", tot["baseline_worst_g"], tot["caribou_worst_g"])):
                b_kg = (base[hw] + b_add) / 1000
                c_kg = (car[hw] + c_add) / 1000
                saving = 1 - c_kg / b_kg
                line[f"baseline_{case}_kg"] = round(b_kg, 2)
                line[f"caribou_{case}_kg"] = round(c_kg, 2)
                line[f"saving_{case}"] = round(saving, 4)
                print(f"  {hw:12s} {case:5s}: Baseline {b_kg:7.2f} kg  Caribou {c_kg:7.2f} kg  "
                      f"saving {saving:6.2%}")
            summary_rows.append(line)
        with open(os.path.join(args.out, "transmission_summary.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            w.writerows(summary_rows)

    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
