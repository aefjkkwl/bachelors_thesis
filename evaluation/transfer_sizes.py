"""
Reads Caribou's workflow_summary_table and prints the measured data volume per
invocation for each workflow (input for transmission carbon, Caribou Eq. 7.5).

Fields from the Caribou logs:
  start_hop_info.data_transfer_size_gb            payload client -> first function
  execution_data[].data_transfer_during_execution_gb
        network traffic from Lambda Insights minus input/output (e.g. S3 download)
  execution_data[].additional_analysis_data.download_size_gb
  execution_data[].additional_analysis_data.total_input_data_transfer_gb
  execution_data[].additional_analysis_data.total_output_data_transfer_gb

Usage (in the Caribou folder, needs AWS credentials for Caribou's DynamoDB tables):
    poetry run python <REPO>/evaluation/transfer_sizes.py --filter sebs_ --out <REPO>/results/transfer_sizes.csv
"""

import argparse
import csv
import json
import statistics
from collections import defaultdict

import boto3
import zstandard as zstd

TABLE_NAME = "workflow_summary_table"
REGION = "us-west-2"  # Caribou's control tables live here
MB = 1024  # GB -> MB


def decode(item):
    value = item["value"]
    if "B" in value:
        return json.loads(zstd.ZstdDecompressor().decompress(value["B"]).decode("utf-8"))
    return json.loads(value["S"])


def scan_all(client):
    kwargs = {"TableName": TABLE_NAME}
    while True:
        resp = client.scan(**kwargs)
        yield from resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def mean_mb(values):
    return round(statistics.mean(values) * MB, 4) if values else ""


def max_mb(values):
    return round(max(values) * MB, 4) if values else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default="sebs_", help="only keys containing this text")
    ap.add_argument("--out", default="transfer_sizes.csv")
    args = ap.parse_args()

    client = boto3.client("dynamodb", region_name=REGION)
    rows = []

    for item in scan_all(client):
        key = item["key"]["S"]
        if args.filter and args.filter not in key:
            continue
        try:
            data = decode(item)
        except Exception as e:  # noqa: BLE001
            print(f"[skip] {key}: {e}")
            continue

        logs = data.get("logs", [])
        start = []
        during = defaultdict(list)  # per region
        download, tin, tout = [], [], []

        for log in logs:
            sh = log.get("start_hop_info") or {}
            if "data_transfer_size_gb" in sh:
                start.append(float(sh["data_transfer_size_gb"]))
            for ex in log.get("execution_data", []):
                reg = ex.get("provider_region", "?")
                during[reg].append(float(ex.get("data_transfer_during_execution_gb", 0.0)))
                add = ex.get("additional_analysis_data", {})
                if "download_size_gb" in add:
                    download.append(float(add["download_size_gb"]))
                if "total_input_data_transfer_gb" in add:
                    tin.append(float(add["total_input_data_transfer_gb"]))
                if "total_output_data_transfer_gb" in add:
                    tout.append(float(add["total_output_data_transfer_gb"]))

        all_during = [v for vs in during.values() for v in vs]
        rows.append({
            "workflow": key,
            "n_logs": len(logs),
            "n_exec": len(all_during),
            "regions": ";".join(f"{r}:{len(v)}" for r, v in sorted(during.items())),
            "start_hop_mb_mean": mean_mb(start),
            "during_exec_mb_mean": mean_mb(all_during),
            "during_exec_mb_max": max_mb(all_during),
            "download_mb_mean": mean_mb(download),
            "input_mb_mean": mean_mb(tin),
            "output_mb_mean": mean_mb(tout),
        })

    rows.sort(key=lambda r: r["workflow"])
    if not rows:
        print("No matching entries found. Check --filter.")
        return

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    for r in rows:
        print(f"{r['workflow']:40s} logs={r['n_logs']:4d}  start={r['start_hop_mb_mean']} MB  "
              f"exec={r['during_exec_mb_mean']} MB  [{r['regions']}]")
    print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
