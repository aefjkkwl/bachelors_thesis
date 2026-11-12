#!/usr/bin/env python3
"""
reconstruct_ecolife.py
======================
Reconstructs from EcoLife's result files (carbon_*.json and st_*.json) how its
emissions are composed: execution vs. idle time (keep-alive), operational vs.
embodied, cold starts, chosen hardware and service time (thesis, Section 4.3).
EcoLife itself only reports total emissions.

Why this works
  For every function and invocation minute, EcoLife writes two values:
    st.json:     service time of all invocations in that minute (execution only)
    carbon.json: carbon of the execution + carbon of the subsequent keep-alive
                 (the idle time is later booked onto the entry of the invocation
                 minute, optimizers/tech.py and exe_decide.py)
  A single invocation has exactly one of four service times (EcoLife's profiles):
    warm on old HW = exe_old, warm on new HW = exe_new,
    cold on old HW = cs+exe_old, cold on new HW = cs+exe_new
  In EcoLife, the hardware for cold starts follows a fixed rule (exe_decide.py,
  cold_old_score < cold_new_score) and can be recomputed from the CI and profiles.
  The service time therefore determines the number of warm and cold starts and the
  hardware. This gives the execution carbon (EcoLife's formulas), and
      idle = carbon of the entry - execution.
  The idle carbon equals (instance minutes) x (idle carbon per minute of the
  hardware at the CI of the invocation minute). If the number of minutes is an
  integer for only one hardware, the keep-alive hardware is known as well and the
  idle carbon can be split into operational and embodied.

Limits (reported as shares, not silently ignored)
  - "unresolved": no combination matches the service time
  - "ambiguous":  several combinations match (one of them is chosen)
  - idle "unclear": minutes are an integer for neither or both hardware types
                    (e.g. when EcoLife's memory limit moves instances); then only
                    the total idle carbon is known

Inputs
  --ecolife-dir   EcoLife repository (function_mem.csv, server_info.json,
                  data/avg_data, selected_trace.zip)
  --results-dir   folder with carbon_*.json and st_*.json (also gzipped, also
                  without a .gz extension), named e.g. carbon_DE_PairA.json,
                  carbon_DE_fix_i3.json, carbon_US-MIDA-PJM_fix_m5zn.json and
                  st_... accordingly
  --ci-de, --ci-us  Electricity Maps CSV (default: ECOLIFE_DIR/carbon_intensity/...)

Output (--out)
  ecolife_breakdown.csv          one row per run
  ecolife_breakdown_per_app.csv  per run and SeBS function

Usage (from the repository root)
  python evaluation/reconstruct_ecolife.py --ecolife-dir <ECOLIFE_REPO> \
      --results-dir <ECOLIFE_RESULTS> \
      --ci-de data/carbon/snapshots_2026-02-10_DE-2025-hourly.csv \
      --ci-us data/carbon/snapshots_2026-02-10_US-MIDA-PJM-2025-hourly.csv \
      --out results/ecolife
"""
import argparse
import csv
import glob
import gzip
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SECONDS_4_YEARS = 4 * 12 * 30 * 24 * 3600   # as in EcoLife
MINUTES_4_YEARS = 4 * 12 * 30 * 24 * 60
EXPECTED_CALLS = 6005186
PAIRS = {"PairA": ("i3", "m5zn"), "fix_i3": ("i3", "i3"), "fix_m5zn": ("m5zn", "m5zn")}


# ------------------------------------------------------------------ EcoLife model
class Model:
    """EcoLife's formulas from utils.py (compute_exe, compute_kat, get_st), split into op/emb."""

    def __init__(self, eco_dir):
        self.mem = {}
        with open(os.path.join(eco_dir, "function_mem.csv"), newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                self.mem[row[0]] = float(row[1])
        with open(os.path.join(eco_dir, "server_info.json"), encoding="utf-8") as f:
            self.info = {s["name"]: s for s in json.load(f)}
        self.prof = {}
        for app in self.mem:
            for srv in ("i3", "m5zn"):
                p = os.path.join(eco_dir, "data", "avg_data", f"{app}_{srv}.metal.json")
                with open(p, encoding="utf-8") as f:
                    self.prof[(app, srv)] = json.load(f)[0]

    def q(self, app, srv):
        return self.mem[app] / self.info[srv]["mem"]

    def st(self, app, srv, cold):
        p = self.prof[(app, srv)]
        return p["cs"] + p["exe"] if cold else p["exe"]

    def exec_parts(self, app, srv, cold, ci):
        """Carbon of one invocation: (operational, embodied)."""
        p, q, info = self.prof[(app, srv)], self.q(app, srv), self.info[srv]
        if cold:
            st = p["cs"] + p["exe"]
            e_cpu = p["cs_energy_cpu"] + p["exe_energy_cpu"]
            e_dram = p["cs_energy_dram"] + p["exe_energy_dram"]
        else:
            st, e_cpu, e_dram = p["exe"], p["exe_energy_cpu"], p["exe_energy_dram"]
        oc = (e_cpu * ci) / 1000 / 3600 + (e_dram * q * ci) / 1000 / 3600
        ec = (st / SECONDS_4_YEARS) * info["ec_cpu"] * 1000 \
            + (st / SECONDS_4_YEARS) * q * info["ec_ram"] * 1000
        return oc, ec

    def idle_rate(self, app, srv, ci):
        """Idle carbon of one instance per minute: (operational, embodied)."""
        q, info = self.q(app, srv), self.info[srv]
        ec = (1000 / MINUTES_4_YEARS) * q * info["ec_ram"] \
            + (1000 / MINUTES_4_YEARS) * (1 / info["core"]) * info["ec_cpu"]
        oc = q * ci * info["dram_idle_energy"] / 1000 / 3600 / 10 \
            + (1 / info["core"]) * ci * info["cpu_idle_energy"] / 1000 / 3600 / 10
        return oc, ec

    def cold_hw(self, app, pair, ci, st_lambda):
        """Hardware for cold starts as in exe_decide.py: index 0 (old) or 1 (new)."""
        old, new = pair
        st_o, st_n = self.st(app, old, True), self.st(app, new, True)
        c_o, c_n = sum(self.exec_parts(app, old, True, ci)), sum(self.exec_parts(app, new, True, ci))
        s_o = st_lambda * (st_o / max(st_n, st_o)) + (1 - st_lambda) * (c_o / max(c_o, c_n))
        s_n = st_lambda * (st_n / max(st_n, st_o)) + (1 - st_lambda) * (c_n / max(c_o, c_n))
        return 0 if s_o < s_n else 1


# ------------------------------------------------------------------ reading files
def open_any(path):
    with open(path, "rb") as f:
        magic = f.read(2)
    return gzip.open(path, "rt", encoding="utf-8") if magic == b"\x1f\x8b" \
        else open(path, encoding="utf-8")


ENTRY_KEY = re.compile(r'^\s*"(\d+)"\s*:\s*\{\s*$')
NUM = re.compile(r'"num"\s*:\s*([0-9.eE+-]+)')
VAL = re.compile(r'"(carbon|st)"\s*:\s*([0-9.eE+-]+)')
FUNC_START = re.compile(r'^    \{\s*$')
FUNC_EMPTY = re.compile(r'^    \{\}\s*,?\s*$')


def stream_entries(path):
    """Yields (function index, minute, num, value) line by line without loading the whole file.
    Expects the format of EcoLife's json.dump(..., indent=4). Otherwise: json.load."""
    func, minute, num, got = -1, None, None, 0
    with open_any(path) as f:
        for line in f:
            if FUNC_START.match(line) or FUNC_EMPTY.match(line):
                func += 1
                continue
            m = ENTRY_KEY.match(line)
            if m:
                minute, num = int(m.group(1)), None
                continue
            m = NUM.search(line)
            if m and minute is not None:
                num = int(float(m.group(1)))
            m = VAL.search(line)
            if m and minute is not None:
                got += 1
                yield func, minute, num, float(m.group(2))
                minute = None
    if got == 0:   # different format: load completely
        with open_any(path) as f:
            data = json.load(f)
        for i, d in enumerate(data):
            for k, v in d.items():
                yield i, int(k), int(v["num"]), float(v.get("carbon", v.get("st")))


def load_apps(eco_dir):
    """Function names in the same order as in EcoLife (sorted trace files)."""
    folder = os.path.join(eco_dir, "selected_trace")
    names = []
    if os.path.isdir(folder) and any(n.endswith(".txt") for n in os.listdir(folder)):
        names = [n for n in os.listdir(folder) if n.endswith(".txt")]
    else:
        with zipfile.ZipFile(os.path.join(eco_dir, "selected_trace.zip")) as z:
            names = [i.filename.rsplit("/", 1)[-1] for i in z.infolist()
                     if i.filename.endswith(".txt") and not i.filename.startswith("__MACOSX/")
                     and not i.filename.rsplit("/", 1)[-1].startswith("._")]
    return [n.split("*")[0] for n in sorted(names)]


def load_ci(path, start, hours, column="Carbon intensity gCO₂eq/kWh (direct)"):
    vals = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["Datetime (UTC)"].replace("Z", "")).replace(tzinfo=timezone.utc)
            vals[ts] = float(row[column])
    return [vals[start + timedelta(hours=h)] for h in range(hours)]


def classify(name):
    n = name.lower()
    region = "DE" if re.search(r"(^|[_\-.])de([_\-.]|$)", n) else (
        "US" if ("pjm" in n or re.search(r"(^|[_\-.])us([_\-.]|$)", n)) else None)
    if "pair" in n:
        variant = "PairA"
    elif "fix_i3" in n:
        variant = "fix_i3"
    elif "fix_m5zn" in n:
        variant = "fix_m5zn"
    else:
        variant = None
    return region, variant


def find_runs(folder):
    runs = defaultdict(dict)
    for name in sorted(os.listdir(folder)):
        low = name.lower()
        kind = "carbon" if low.startswith("carbon_") else ("st" if low.startswith("st_") else None)
        if kind is None:
            continue
        region, variant = classify(name)
        if region and variant:
            if kind in runs[(region, variant)]:
                sys.exit(f"Several {kind} files for {region} {variant}")
            runs[(region, variant)][kind] = os.path.join(folder, name)
    return runs


# ------------------------------------------------------------------ reconstruction
def new_acc():
    return defaultdict(float)


def reconstruct(model, apps, ci_hour, carbon_path, st_path, pair, st_lambda):
    tot = new_acc()
    per_app = defaultdict(new_acc)
    tol = 1e-6
    cache = {}
    st_iter = stream_entries(st_path)
    for (fi, minute, num, carbon) in stream_entries(carbon_path):
        si, sminute, snum, st_obs = next(st_iter)
        if (fi, minute, num) != (si, sminute, snum):
            sys.exit(f"carbon and st files do not match at function {fi}, minute {minute}")
        app = apps[fi]
        ci = ci_hour[minute // 60]
        key = (app, minute // 60)
        if key not in cache:
            c = model.cold_hw(app, pair, ci, st_lambda)
            cache[key] = {
                "cold_hw": c,
                "st": [model.st(app, pair[0], False), model.st(app, pair[1], False),
                       model.st(app, pair[c], True)],
                "warm": [model.exec_parts(app, pair[0], False, ci),
                         model.exec_parts(app, pair[1], False, ci)],
                "cold": model.exec_parts(app, pair[c], True, ci),
                "idle": [model.idle_rate(app, pair[0], ci), model.idle_rate(app, pair[1], ci)],
            }
        k = cache[key]
        sw_o, sw_n, s_c = k["st"]

        # matching combinations (warm old, warm new), the rest is cold
        matches = []
        same_hw = pair[0] == pair[1]   # fixed hardware: "old" and "new" are the same machine
        for w_o in range(num + 1):
            for w_n in (range(1) if same_hw else range(num - w_o + 1)):
                pred = w_o * sw_o + w_n * sw_n + (num - w_o - w_n) * s_c
                if abs(pred - st_obs) <= tol * max(1.0, st_obs):
                    matches.append((w_o, w_n))
        a = per_app[app]
        for acc in (tot, a):
            acc["calls"] += num
            acc["entries"] += 1
            acc["total_g"] += carbon
            acc["st_s"] += st_obs
        if not matches:
            for acc in (tot, a):
                acc["unresolved_entries"] += 1
                acc["unresolved_calls"] += num
                acc["unresolved_g"] += carbon
            continue
        if len(matches) > 1:
            for acc in (tot, a):
                acc["ambiguous_entries"] += 1
            # preferred: warm invocations on one hardware only (EcoLife keeps warm in one place)
            matches.sort(key=lambda m: (m[0] > 0 and m[1] > 0, -(m[0] + m[1])))
        w_o, w_n = matches[0]
        cold = num - w_o - w_n
        ex_oc = w_o * k["warm"][0][0] + w_n * k["warm"][1][0] + cold * k["cold"][0]
        ex_ec = w_o * k["warm"][0][1] + w_n * k["warm"][1][1] + cold * k["cold"][1]
        idle = carbon - ex_oc - ex_ec

        on_new = w_n + (cold if k["cold_hw"] == 1 else 0)
        for acc in (tot, a):
            acc["cold"] += cold
            acc["warm"] += w_o + w_n
            acc["calls_on_new"] += on_new
            acc["exec_oc"] += ex_oc
            acc["exec_ec"] += ex_ec
            acc["idle_g"] += idle
            if idle < -1e-9 * max(1.0, carbon):
                acc["negative_idle_entries"] += 1

        # idle time: instance minutes must be an integer
        if abs(idle) <= 1e-12:
            continue
        cand = []
        for h in (0, 1):
            oc_r, ec_r = k["idle"][h]
            m = idle / (oc_r + ec_r)
            if m > 0 and abs(m - round(m)) <= 1e-4 * max(1.0, m):
                cand.append((h, round(m)))
        if pair[0] == pair[1] and cand:
            cand = cand[:1]
        if len(cand) == 1:
            h, m = cand[0]
            oc_r, ec_r = k["idle"][h]
            for acc in (tot, a):
                acc["idle_oc"] += m * oc_r
                acc["idle_ec"] += m * ec_r
                acc["idle_min"] += m
                if h == 1:
                    acc["idle_min_on_new"] += m
        else:
            for acc in (tot, a):
                acc["idle_unclear_g"] += idle
    for _ in st_iter:
        sys.exit("st file has more entries than the carbon file")
    return tot, per_app


def finish(acc, variant):
    r = dict(acc)
    calls = r.get("calls", 0) or 1
    r["exec_g"] = r.get("exec_oc", 0) + r.get("exec_ec", 0)
    r["cold_share"] = r.get("cold", 0) / calls
    r["share_calls_on_m5zn"] = r.get("calls_on_new", 0) / calls if variant == "PairA" else (
        1.0 if variant == "fix_m5zn" else 0.0)
    idle_min = r.get("idle_min", 0)
    r["share_idle_min_on_m5zn"] = (r.get("idle_min_on_new", 0) / idle_min if idle_min else 0.0) \
        if variant == "PairA" else (1.0 if variant == "fix_m5zn" else 0.0)
    r["avg_service_time_s"] = r.get("st_s", 0) / calls
    r["unresolved_share_g"] = r.get("unresolved_g", 0) / r["total_g"] if r.get("total_g") else 0.0
    r["idle_unclear_share"] = (r.get("idle_unclear_g", 0) / r["idle_g"]) if r.get("idle_g") else 0.0
    return r


COLS = ["calls", "total_g", "exec_g", "exec_oc", "exec_ec", "idle_g", "idle_oc", "idle_ec",
        "idle_min", "cold", "cold_share", "share_calls_on_m5zn", "share_idle_min_on_m5zn",
        "avg_service_time_s", "entries", "ambiguous_entries", "unresolved_entries",
        "unresolved_share_g", "negative_idle_entries", "idle_unclear_share"]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ecolife-dir", required=True)
    ap.add_argument("--results-dir", required=True)
    ap.add_argument("--ci-de", default=None)
    ap.add_argument("--ci-us", default=None)
    ap.add_argument("--start", default="2025-08-25T00:00:00")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--st-lambda", type=float, default=0.5, help="as in EcoLife main.py (STlambda)")
    ap.add_argument("--only", nargs="+", default=None, help="e.g. DE_PairA US_fix_i3")
    ap.add_argument("--out", default=".")
    a = ap.parse_args()

    def ci_path(given, pattern):
        if given:
            return given
        hits = glob.glob(os.path.join(a.ecolife_dir, "carbon_intensity", pattern))
        if not hits:
            sys.exit(f"No CI file {pattern} in {a.ecolife_dir}/carbon_intensity. "
                     f"Please specify it with --ci-de / --ci-us.")
        return hits[0]

    start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
    hours = a.days * 24
    ci = {"DE": load_ci(ci_path(a.ci_de, "DE*2025_hourly.csv*"), start, hours),
          "US": load_ci(ci_path(a.ci_us, "US-MIDA-PJM*2025_hourly.csv*"), start, hours)}
    model = Model(a.ecolife_dir)
    apps = load_apps(a.ecolife_dir)
    print(f"Trace functions: {len(apps)}")

    runs = find_runs(a.results_dir)
    os.makedirs(a.out, exist_ok=True)
    rows, app_rows = [], []
    for (region, variant), files in sorted(runs.items()):
        name = f"{region}_{variant}"
        if a.only and name not in a.only:
            continue
        if "carbon" not in files or "st" not in files:
            print(f"  skipped {name}: {'st' if 'carbon' in files else 'carbon'} file missing")
            continue
        print(f"Reconstructing {name} ({os.path.basename(files['carbon'])}, "
              f"{os.path.basename(files['st'])}) ...", flush=True)
        tot, per_app = reconstruct(model, apps, ci[region], files["carbon"], files["st"],
                                   PAIRS[variant], a.st_lambda)
        r = finish(tot, variant)
        rows.append({"run": name, "region": region, "variant": variant, **{c: r.get(c, 0) for c in COLS}})
        for app_name, acc in sorted(per_app.items()):
            ra = finish(acc, variant)
            app_rows.append({"run": name, "app": app_name, **{c: ra.get(c, 0) for c in COLS}})
        warn = "" if int(r["calls"]) == EXPECTED_CALLS else f"  WARNING: expected {EXPECTED_CALLS}"
        print(f"  calls {int(r['calls'])}{warn}")
        print(f"  total {r['total_g'] / 1000:.2f} kg = execution {r['exec_g'] / 1000:.2f} "
              f"+ idle {r['idle_g'] / 1000:.2f} (+ unresolved {r.get('unresolved_g', 0) / 1000:.2f})")
        print(f"  cold starts {r['cold_share']:.2%}, calls on m5zn {r['share_calls_on_m5zn']:.1%}, "
              f"service time {r['avg_service_time_s']:.2f} s")
        print(f"  unresolved {r['unresolved_share_g']:.3%} of the carbon, "
              f"ambiguous {int(r.get('ambiguous_entries', 0))} entries, "
              f"idle unclear {r['idle_unclear_share']:.2%}")

    for fname, data in (("ecolife_breakdown.csv", rows), ("ecolife_breakdown_per_app.csv", app_rows)):
        if not data:
            continue
        with open(os.path.join(a.out, fname), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            w.writeheader()
            w.writerows(data)
    print(f"\nSaved in {a.out}: ecolife_breakdown.csv, ecolife_breakdown_per_app.csv")


if __name__ == "__main__":
    main()
