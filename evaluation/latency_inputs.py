"""
Reads the inputs for a post-hoc latency model from Caribou's DynamoDB tables.

1. performance_region_table: network latency between regions (CloudPing distribution
   that Caribou itself uses for planning).  -> network_latency.csv
2. workflow_summary_table: measured execution duration (duration_s) per workflow,
   version and region.  -> durations.csv

Why 2.?
   The SeBS data bucket is in us-east-1. When a function runs in eu-central-1
   (version 2.0.0), it downloads its data across the Atlantic. The duration
   difference EU minus US is therefore the measured penalty for cross-region data
   access. Caribou itself does not account for this penalty
   (relative_performance = 1, data transfer during execution only enters carbon).

   Control: the graph functions load hardly any data (< 0.03 MB). Their EU/US ratio
   shows whether the regions differ in speed even without data.
   penalty_corr_s removes this region factor.

Usage (in the Caribou folder, needs AWS credentials for Caribou's DynamoDB tables):
    poetry run python <REPO>/evaluation/latency_inputs.py --out-dir <REPO>/results/latency
"""

import argparse
import csv
import json
import os
import statistics
from collections import defaultdict

import boto3
import zstandard as zstd

REGION = "us-west-2"  # Caribou's control tables live here
SUMMARY_TABLE = "workflow_summary_table"
PERFORMANCE_TABLE = "performance_region_table"
DATA_REGION = "aws:us-east-1"     # location of the SeBS bucket
OTHER_REGION = "aws:eu-central-1"
CONTROL = ("sebs_graph_bfs", "sebs_graph_mst", "sebs_graph_pagerank")


def decode(item):
    value = item["value"]
    if "B" in value:
        return json.loads(zstd.ZstdDecompressor().decompress(value["B"]).decode("utf-8"))
    return json.loads(value["S"])


def scan_all(client, table):
    kwargs = {"TableName": table}
    while True:
        resp = client.scan(**kwargs)
        yield from resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def stats(values):
    if not values:
        return {"n": 0, "mean": "", "median": "", "p95": ""}
    v = sorted(values)
    return {"n": len(v), "mean": statistics.mean(v), "median": statistics.median(v),
            "p95": v[min(len(v) - 1, int(round(0.95 * (len(v) - 1))))]}


def network_latency(client, out_dir):
    rows = []
    for item in scan_all(client, PERFORMANCE_TABLE):
        key = item["key"]["S"]
        try:
            data = decode(item)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {key}: {e}")
            continue
        for to, info in data.get("transmission_latency", {}).items():
            if {key, to} <= {DATA_REGION, OTHER_REGION}:
                s = stats(info.get("latency_distribution", []))
                rows.append({"from": key, "to": to, "n": s["n"],
                             "mean_ms": round(s["mean"] * 1000, 2) if s["n"] else "",
                             "median_ms": round(s["median"] * 1000, 2) if s["n"] else "",
                             "p95_ms": round(s["p95"] * 1000, 2) if s["n"] else ""})
    path = os.path.join(out_dir, "network_latency.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["from", "to", "n", "mean_ms", "median_ms", "p95_ms"])
        w.writeheader()
        w.writerows(rows)
    print("\n1) Network latency (CloudPing, as used by Caribou for planning)")
    for r in rows:
        print(f"  {r['from']:18s} -> {r['to']:18s} mean {r['mean_ms']} ms  median {r['median_ms']} ms  "
              f"p95 {r['p95_ms']} ms")
    if not rows:
        print("  no entries found for us-east-1/eu-central-1")


def durations(client, out_dir, key_filter):
    # dur[workflow][region] = list of duration_s over all versions in this region
    dur = defaultdict(lambda: defaultdict(list))
    start_hop = defaultdict(lambda: defaultdict(list))
    versions = defaultdict(lambda: defaultdict(set))
    for item in scan_all(client, SUMMARY_TABLE):
        key = item["key"]["S"]
        if key_filter not in key:
            continue
        wf, version = key.rsplit("-", 1)
        try:
            data = decode(item)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {key}: {e}")
            continue
        for log in data.get("logs", []):
            sh = log.get("start_hop_info") or {}
            for ex in log.get("execution_data", []):
                reg = ex.get("provider_region", "?")
                if "duration_s" in ex:
                    dur[wf][reg].append(float(ex["duration_s"]))
                    versions[wf][reg].add(version)
                lat = sh.get("latency_from_client_s")
                if lat and 0 < float(lat) < 3:
                    start_hop[wf][reg].append(float(lat))

    # region factor from the control functions (median EU / median US)
    ratios = []
    for wf in CONTROL:
        us, eu = dur[wf].get(DATA_REGION), dur[wf].get(OTHER_REGION)
        if us and eu:
            ratios.append(statistics.median(eu) / statistics.median(us))
    factor = statistics.mean(ratios) if ratios else 1.0

    rows = []
    for wf in sorted(dur):
        us, eu = stats(dur[wf].get(DATA_REGION, [])), stats(dur[wf].get(OTHER_REGION, []))
        if not (us["n"] and eu["n"]):
            continue
        rows.append({
            "workflow": wf,
            "versions_us": " ".join(sorted(versions[wf][DATA_REGION])),
            "versions_eu": " ".join(sorted(versions[wf][OTHER_REGION])),
            "n_us": us["n"], "n_eu": eu["n"],
            "median_us_s": round(us["median"], 4), "median_eu_s": round(eu["median"], 4),
            "mean_us_s": round(us["mean"], 4), "mean_eu_s": round(eu["mean"], 4),
            "penalty_s": round(eu["median"] - us["median"], 4),
            "penalty_corr_s": round(eu["median"] - us["median"] * factor, 4),
            "start_hop_client_us_s": round(statistics.median(start_hop[wf][DATA_REGION]), 4)
            if start_hop[wf][DATA_REGION] else "",
            "start_hop_client_eu_s": round(statistics.median(start_hop[wf][OTHER_REGION]), 4)
            if start_hop[wf][OTHER_REGION] else "",
        })
    path = os.path.join(out_dir, "durations.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["workflow"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n2) Measured duration per execution (median), data in {DATA_REGION}")
    print(f"   Region factor EU/US from graph functions: {factor:.3f} "
          f"(individual: {', '.join(f'{r:.3f}' for r in ratios)})")
    for r in rows:
        print(f"  {r['workflow']:28s} US {r['median_us_s']:8.3f} s (n={r['n_us']:3d})  "
              f"EU {r['median_eu_s']:8.3f} s (n={r['n_eu']:3d})  "
              f"penalty {r['penalty_s']:+8.3f} s  corrected {r['penalty_corr_s']:+8.3f} s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--filter", default="sebs_")
    ap.add_argument("--out-dir", default="latency")
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    client = boto3.client("dynamodb", region_name=REGION)
    network_latency(client, args.out_dir)
    durations(client, args.out_dir, args.filter)
    print(f"\n-> {args.out_dir}")


if __name__ == "__main__":
    main()
