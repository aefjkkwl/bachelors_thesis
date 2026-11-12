"""
evaluate_carbon.py
==================
Computes the emissions of the Baseline and Caribou setups with EcoLife's carbon
model and the Azure Functions trace (thesis, Sections 3.3.1 and 3.3.2).

For every Azure function and every minute of the trace:
  1. Region: from placements.csv (Caribou's plan for this SeBS function in this
             hour) or a fixed region (--fixed-region, for the baselines).
  2. Number of invocations n: from the trace.
  3. Warm or cold, depending on --pool-rule. Under both rules an instance is warm
     if at most KAT minutes have passed since its last invocation, and concurrent
     invocations within one minute each need their own instance.

     --pool-rule ecolife  (as in EcoLife, optimizers/tech.py and exe_decide.py;
                           used for all results in the thesis)
         After a minute with n invocations, exactly n instances stay warm, in the
         region of that invocation. Older instances (also in another region) are
         discarded at the next invocation; their idle time until then is counted.
         At most as many invocations are warm as instances are warm in the same
         region, the rest is cold.

     --pool-rule lambda   (container pool, closer to the behaviour of AWS Lambda;
                           not used in the thesis)
         A pool of warm containers is kept per Azure function and region. Each
         container stays warm for up to KAT minutes after its own last invocation,
         even if fewer invocations arrive in between. Invocations up to the number
         of warm containers are warm, the rest is cold and creates new containers.
         The most recently used containers are reused first.
  4. Carbon, split into operational and embodied:
       - execution (warm, or cold including the cold-start overhead), with the
         carbon intensity (CI) of the region in that hour
       - idle time (keep-alive): waiting time of an instance until the next
         invocation, at most KAT minutes, with the CI at the start of the idle
         period (as in EcoLife)

All formulas are taken unchanged from EcoLife's utils.py (compute_exe,
compute_kat); only the operational and embodied parts are reported separately.
Unit: gCO2eq.

Settings as in the EcoLife runs (main.py / utils.py):
  carbon column "direct", start 2025-08-25 00:00 UTC, 7 days, first 10,080 trace
  minutes, hardware i3 (old) / m5zn (new), evaluated from minute 20 (window_size).

Inputs:
  --ecolife-dir   EcoLife repository (function_mem.csv, server_info.json,
                  data/avg_data/, selected_trace.zip)
  --placements    Caribou's hourly plan from replay_caribou_week.py
  --ci            hourly Electricity Maps CSV per region (data/carbon/)

Usage (from the repository root; the results in the thesis):
  python evaluation/evaluate_carbon.py --ecolife-dir <ECOLIFE_REPO> \
      --fixed-region eu-central-1 \
      --ci eu-central-1=data/carbon/snapshots_2026-02-10_DE-2025-hourly.csv \
      --pool-rule ecolife --kat 0 5 10 15 20 25 30 --hardware old new \
      --out results/baseline_caribou_5minKAT/baseline_eu_ecolife

  python evaluation/evaluate_carbon.py --ecolife-dir <ECOLIFE_REPO> \
      --placements results/caribou_replay_home_us/placements.csv \
      --ci eu-central-1=data/carbon/snapshots_2026-02-10_DE-2025-hourly.csv \
      --ci us-east-1=data/carbon/snapshots_2026-02-10_US-MIDA-PJM-2025-hourly.csv \
      --pool-rule ecolife --kat 0 5 10 15 20 25 30 --hardware old new \
      --out results/baseline_caribou_5minKAT/caribou_home_us_ecolife

Output (folder --out):
  summary.csv     one row per scenario (KAT x hardware)
  per_app.csv     per scenario and SeBS function
  per_region.csv  per scenario and region
  per_hour.csv    per scenario and hour
  settings.json   all settings of this run
"""
import argparse
import csv
import json
import math
import os
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SECONDS_4_YEARS = 4 * 12 * 30 * 24 * 3600   # as in EcoLife
MINUTES_4_YEARS = 4 * 12 * 30 * 24 * 60     # as in EcoLife

# EcoLife name -> Caribou workflow name
APP_TO_WORKFLOW = {
    "dynamic-100k": "sebs_dynamic_html",
    "upload-img": "sebs_uploader",
    "thumbnailer": "sebs_thumbnailer",
    "video": "sebs_video_processing",
    "compression": "sebs_compression",
    "image-recog": "sebs_image_recognition",
    "pagerank-1000k": "sebs_graph_pagerank",
    "mst-1000k": "sebs_graph_mst",
    "bfs-1000k": "sebs_graph_bfs",
    "dna": "sebs_dna_visualisation",
}


# --------------------------------------------------------------------------- #
# Loading inputs
# --------------------------------------------------------------------------- #
def load_function_mem(ecolife_dir):
    mem = {}
    with open(os.path.join(ecolife_dir, "function_mem.csv"), newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            mem[row[0]] = float(row[1])
    return mem


def load_server_info(ecolife_dir, name):
    with open(os.path.join(ecolife_dir, "server_info.json"), encoding="utf-8") as f:
        for s in json.load(f):
            if s["name"] == name:
                return s
    raise SystemExit(f"Server {name} not found in server_info.json")


def load_profile(ecolife_dir, app, server):
    path = os.path.join(ecolife_dir, "data", "avg_data", f"{app}_{server}.metal.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)[0]


def load_traces(trace_dir, total_minutes):
    """As read_selected_traces() in utils.py: sorted, first total_minutes rows.

    trace_dir can be a folder or selected_trace.zip itself. The file names contain
    a "*" (e.g. bfs-1000k*115.txt), which is not allowed on Windows. The script
    therefore reads the zip file directly without extracting it.
    """
    entries = []  # (file name, content)
    if trace_dir.lower().endswith(".zip"):
        with zipfile.ZipFile(trace_dir) as z:
            for info in z.infolist():
                fn = info.filename.rsplit("/", 1)[-1]
                # skip macOS metadata in the zip (__MACOSX/._name)
                if info.filename.startswith("__MACOSX/") or fn.startswith("._"):
                    continue
                if fn.endswith(".txt"):
                    entries.append((fn, z.read(info).decode("utf-8")))
    else:
        for fn in os.listdir(trace_dir):
            if fn.endswith(".txt"):
                with open(os.path.join(trace_dir, fn), encoding="utf-8") as f:
                    entries.append((fn, f.read()))
    names, traces = [], []
    for fn, text in sorted(entries):
        names.append(fn.split("*")[0])
        traces.append([int(float(x)) for x in text.split()][:total_minutes])
    return names, traces


def load_ci(csv_path, column, start, hours):
    """Hourly CI from start (UTC) for `hours` hours."""
    values = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["Datetime (UTC)"].replace("Z", "")).replace(tzinfo=timezone.utc)
            values[ts] = float(row[column])
    out = []
    for h in range(hours):
        ts = start + timedelta(hours=h)
        if ts not in values:
            raise SystemExit(f"{csv_path}: no value for {ts.isoformat()}")
        out.append(values[ts])
    return out


def load_placements(path, start, hours):
    """{workflow_name: [region per hour]}"""
    plan = defaultdict(lambda: [None] * hours)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            day = datetime.fromisoformat(row["sim_date"]).replace(tzinfo=timezone.utc)
            h = int((day - start).total_seconds() // 3600) + int(row["hour_utc"])
            if 0 <= h < hours:
                wf_name = row["workflow_id"].rsplit("-", 1)[0]
                plan[wf_name][h] = row["region"]
    for wf, regions in plan.items():
        missing = [h for h, r in enumerate(regions) if r is None]
        if missing:
            raise SystemExit(f"placements.csv: {wf} has no region for {len(missing)} hours")
    return dict(plan)


# --------------------------------------------------------------------------- #
# EcoLife formulas (from utils.py), split into operational / embodied
# --------------------------------------------------------------------------- #
class EcoLifeModel:
    def __init__(self, ecolife_dir, server, func_mem):
        self.info = load_server_info(ecolife_dir, server)
        self.server = server
        self.profiles = {app: load_profile(ecolife_dir, app, server) for app in func_mem}
        self.q = {app: func_mem[app] / self.info["mem"] for app in func_mem}

    def execution(self, app, ci, cold):
        """Carbon of one invocation (compute_exe). Returns (operational, embodied, service_time_s)."""
        p, q, info = self.profiles[app], self.q[app], self.info
        if cold:
            st = p["cs"] + p["exe"]
            e_cpu = p["cs_energy_cpu"] + p["exe_energy_cpu"]
            e_dram = p["cs_energy_dram"] + p["exe_energy_dram"]
        else:
            st = p["exe"]
            e_cpu = p["exe_energy_cpu"]
            e_dram = p["exe_energy_dram"]
        oc = (e_cpu * ci) / 1000 / 3600 + (e_dram * q * ci) / 1000 / 3600
        ec = (st / SECONDS_4_YEARS) * info["ec_cpu"] * 1000 + (st / SECONDS_4_YEARS) * q * info["ec_ram"] * 1000
        return oc, ec, st

    def keep_alive(self, app, kat_min, ci):
        """Carbon of kat_min minutes of idle time (compute_kat). Returns (operational, embodied)."""
        q, info = self.q[app], self.info
        ec = (kat_min * 1000 / MINUTES_4_YEARS) * q * info["ec_ram"] \
            + (kat_min * 1000 / MINUTES_4_YEARS) * (1 / info["core"]) * info["ec_cpu"]
        oc = kat_min * q * ci * info["dram_idle_energy"] / 1000 / 3600 / 10 \
            + kat_min * (1 / info["core"]) * ci * info["cpu_idle_energy"] / 1000 / 3600 / 10
        return oc, ec


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #
def new_bucket():
    return {"invocations": 0, "cold": 0, "warm": 0, "service_time_s": 0.0,
            "exec_oc": 0.0, "exec_ec": 0.0, "idle_oc": 0.0, "idle_ec": 0.0, "idle_min": 0.0}


def add(bucket, **kw):
    for k, v in kw.items():
        bucket[k] += v


def simulate(app_names, traces, region_of, ci, model, kat, start_minute, total_minutes,
             pool_rule="lambda"):
    """
    region_of(app, hour) -> region; ci[region][hour] -> gCO2/kWh.
    kat in minutes, math.inf = until the next invocation.
    pool_rule: "ecolife" or "lambda" (see the description at the top).
    """
    total = new_bucket()
    per_app = defaultdict(new_bucket)
    per_region = defaultdict(new_bucket)
    per_hour = defaultdict(new_bucket)

    def charge_idle(app, region, since, minutes):
        if minutes <= 0:
            return
        h = since // 60
        oc, ec = model.keep_alive(app, minutes, ci[region][h])
        for b in (total, per_app[app], per_region[region], per_hour[h]):
            add(b, idle_oc=oc, idle_ec=ec, idle_min=minutes)

    exec_cache = {}

    for app, trace in zip(app_names, traces):
        pools = defaultdict(list)   # lambda: region -> list of last invocation minutes (warm containers)
        eco_pool = None             # ecolife: (region, number of instances, minute) or None
        for t in range(start_minute, total_minutes):
            n = trace[t]
            if n == 0:
                continue
            h = t // 60
            region = region_of(app, h)

            if pool_rule == "ecolife":
                # as in EcoLife: a pool entry has exactly as many instances as invocations
                # in its minute (tech.py, "num": traces[i][j]) and is deleted and created
                # anew after every invocation (exe_decide.py, lines 146-149).
                warm = 0
                if eco_pool is not None:
                    p_region, p_n, p_t = eco_pool
                    gap = t - p_t
                    if gap <= kat:
                        # all instances waited until now (exe_decide.py, lines 46-58)
                        charge_idle(app, p_region, p_t, p_n * gap)
                        if p_region == region:
                            warm = min(n, p_n)
                    else:
                        # expired: full KAT of idle time (tech.py, lines 88-117)
                        charge_idle(app, p_region, p_t, p_n * kat)
                cold = n - warm
                eco_pool = (region, n, t)
            else:
                pool = pools[region]
                # expired containers: KAT minutes of idle time, then removed
                alive = []
                for last in pool:
                    if t - last <= kat:
                        alive.append(last)
                    else:
                        charge_idle(app, region, last, kat)
                alive.sort(reverse=True)            # most recently used first
                warm = min(n, len(alive))
                cold = n - warm
                for last in alive[:warm]:           # reused: idle time = gap
                    charge_idle(app, region, last, t - last)
                pools[region] = [t] * n + alive[warm:]

            ci_now = ci[region][h]
            key = (app, region, h)
            if key not in exec_cache:
                exec_cache[key] = (model.execution(app, ci_now, True), model.execution(app, ci_now, False))
            (c_oc, c_ec, c_st), (w_oc, w_ec, w_st) = exec_cache[key]
            vals = dict(invocations=n, cold=cold, warm=warm,
                        service_time_s=cold * c_st + warm * w_st,
                        exec_oc=cold * c_oc + warm * w_oc,
                        exec_ec=cold * c_ec + warm * w_ec)
            for b in (total, per_app[app], per_region[region], per_hour[h]):
                add(b, **vals)

        # end of the period: remaining instances are idle until KAT or the end of the period
        # (EcoLife itself does not count this remainder; the difference is very small)
        for region, pool in pools.items():
            for last in pool:
                charge_idle(app, region, last, min(kat, total_minutes - last))
        if eco_pool is not None:
            p_region, p_n, p_t = eco_pool
            charge_idle(app, p_region, p_t, p_n * min(kat, total_minutes - p_t))

    return total, per_app, per_region, per_hour


def finish(b):
    b = dict(b)
    b["operational_g"] = b["exec_oc"] + b["idle_oc"]
    b["embodied_g"] = b["exec_ec"] + b["idle_ec"]
    b["total_g"] = b["operational_g"] + b["embodied_g"]
    b["cold_share"] = b["cold"] / b["invocations"] if b["invocations"] else 0.0
    b["avg_service_time_s"] = b["service_time_s"] / b["invocations"] if b["invocations"] else 0.0
    return b


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ecolife-dir", required=True)
    ap.add_argument("--trace-dir", help="folder or selected_trace.zip (default: ECOLIFE_DIR/selected_trace, otherwise ECOLIFE_DIR/selected_trace.zip)")
    ap.add_argument("--placements", help="placements.csv from the Caribou replay")
    ap.add_argument("--fixed-region", help="always use this region instead of Caribou's plan (baseline)")
    ap.add_argument("--ci", nargs="+", action="extend", required=True, metavar="REGION=CSV",
                    help="carbon CSV per region, e.g. eu-central-1=DE_2025_hourly.csv")
    ap.add_argument("--column", default="Carbon intensity gCO₂eq/kWh (direct)")
    ap.add_argument("--start", default="2025-08-25T00:00:00", help="trace minute 0 in UTC")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start-minute", type=int, default=20, help="as EcoLife's window_size")
    ap.add_argument("--kat", nargs="+", default=["0", "10", "inf"],
                    help="keep-alive time in minutes, 'inf' = until the next invocation")
    ap.add_argument("--hardware", nargs="+", default=["old", "new"], choices=["old", "new"])
    ap.add_argument("--server-pair", nargs=2, default=["i3", "m5zn"], metavar=("OLD", "NEW"))
    ap.add_argument("--pool-rule", required=True, choices=["ecolife", "lambda"],
                    help="warm/cold rule: 'ecolife' (as in EcoLife, used in the thesis) or 'lambda' (container pool)")
    ap.add_argument("--out", default="results_caribou")
    args = ap.parse_args()

    if bool(args.placements) == bool(args.fixed_region):
        raise SystemExit("Specify exactly one of --placements OR --fixed-region")

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    hours = args.days * 24
    total_minutes = hours * 60

    ci = {}
    for spec in args.ci:
        region, path = spec.split("=", 1)
        ci[region] = load_ci(path, args.column, start, hours)

    func_mem = load_function_mem(args.ecolife_dir)
    trace_dir = args.trace_dir
    if trace_dir is None:  # extracted folder first, otherwise the zip file from the repository
        trace_dir = os.path.join(args.ecolife_dir, "selected_trace")
        if not os.path.isdir(trace_dir):
            trace_dir = os.path.join(args.ecolife_dir, "selected_trace.zip")
    if not (os.path.isdir(trace_dir) or (trace_dir.lower().endswith(".zip") and os.path.isfile(trace_dir))):
        raise SystemExit(f"Traces not found: {trace_dir}")
    app_names, traces = load_traces(trace_dir, total_minutes)
    print(f"Traces read: {len(traces)} from {trace_dir}", flush=True)
    if not traces:
        raise SystemExit(f"No trace files (*.txt) in {trace_dir}. Is this an empty or partly "
                         "extracted folder? Then delete it; the script reads selected_trace.zip directly.")
    if any(len(t) < total_minutes for t in traces):
        raise SystemExit("Trace shorter than the evaluation period")

    if args.placements:
        plan = load_placements(args.placements, start, hours)
        for app in set(app_names):
            if APP_TO_WORKFLOW[app] not in plan:
                raise SystemExit(f"placements.csv contains no plan for {APP_TO_WORKFLOW[app]}")
        regions_used = {r for regs in plan.values() for r in regs}
        def region_of(app, h):
            return plan[APP_TO_WORKFLOW[app]][h]
        scenario_name = "caribou"
    else:
        regions_used = {args.fixed_region}
        def region_of(app, h):
            return args.fixed_region
        scenario_name = f"fixed_{args.fixed_region}"
    missing_ci = regions_used - set(ci)
    if missing_ci:
        raise SystemExit(f"No carbon CSV for region(s): {sorted(missing_ci)}")

    os.makedirs(args.out, exist_ok=True)
    rows_summary, rows_app, rows_region, rows_hour = [], [], [], []
    hw_server = {"old": args.server_pair[0], "new": args.server_pair[1]}

    for hw in args.hardware:
        model = EcoLifeModel(args.ecolife_dir, hw_server[hw], func_mem)
        for kat_str in args.kat:
            kat = math.inf if kat_str.lower() == "inf" else float(kat_str)
            label = {"scenario": scenario_name, "pool_rule": args.pool_rule,
                     "hardware": f"{hw} ({hw_server[hw]})", "kat_min": kat_str}
            print(f"Computing {scenario_name}, rule {args.pool_rule}, hardware {hw} ({hw_server[hw]}), "
                  f"KAT {kat_str} ...", flush=True)
            total, per_app, per_region, per_hour = simulate(
                app_names, traces, region_of, ci, model, kat, args.start_minute, total_minutes,
                args.pool_rule)
            rows_summary.append({**label, **finish(total)})
            rows_app += [{**label, "app": a, **finish(b)} for a, b in sorted(per_app.items())]
            rows_region += [{**label, "region": r, **finish(b)} for r, b in sorted(per_region.items())]
            rows_hour += [{**label, "hour": h,
                           "datetime_utc": (start + timedelta(hours=h)).isoformat(), **finish(b)}
                          for h, b in sorted(per_hour.items())]

    write_csv(os.path.join(args.out, "summary.csv"), rows_summary)
    write_csv(os.path.join(args.out, "per_app.csv"), rows_app)
    write_csv(os.path.join(args.out, "per_region.csv"), rows_region)
    write_csv(os.path.join(args.out, "per_hour.csv"), rows_hour)
    with open(os.path.join(args.out, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({**vars(args), "trace_functions": len(app_names),
                   "unit": "gCO2eq (EcoLife formulas)"}, f, indent=2, ensure_ascii=False)

    print(f"\n{'Hardware':14s} {'KAT':>4s} {'Calls':>9s} {'cold %':>7s} "
          f"{'operat. g':>11s} {'embodied g':>11s} {'total g':>10s}")
    for r in rows_summary:
        print(f"{r['hardware']:14s} {r['kat_min']:>4s} {r['invocations']:9d} {100 * r['cold_share']:6.1f}% "
              f"{r['operational_g']:11.1f} {r['embodied_g']:11.1f} {r['total_g']:10.1f}")
    print(f"\nResults in {args.out}/")


if __name__ == "__main__":
    main()
