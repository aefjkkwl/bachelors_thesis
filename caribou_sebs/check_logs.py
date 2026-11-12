import argparse
import time

import boto3


def count(logs, group, pattern, start_ms):
    n, first = 0, ""
    paginator = logs.get_paginator("filter_log_events")
    try:
        for page in paginator.paginate(logGroupName=group, startTime=start_ms, filterPattern=pattern):
            for e in page["events"]:
                n += 1
                if not first:
                    first = e["message"].strip()
    except logs.exceptions.ResourceNotFoundException:
        return -1, ""
    return n, first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=2)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--prefix", default="sebs_")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=args.region)
    logs = boto3.client("logs", region_name=args.region)
    start_ms = int((time.time() - args.hours * 3600) * 1000)

    functions = sorted(f["FunctionName"] for page in lam.get_paginator("list_functions").paginate()
                       for f in page["Functions"] if f["FunctionName"].startswith(args.prefix))
    if not functions:
        print(f"Keine Lambda-Funktionen mit Praefix '{args.prefix}' in {args.region} gefunden.")
        return

    print(f"Letzte {args.hours:g} h, Region {args.region}\n")
    print(f"{'Funktion':60s} {'OK':>4s} {'Fehler':>7s} {'Kalt':>5s}")
    problems = []
    for fn in functions:
        group = f"/aws/lambda/{fn}"
        ok, _ = count(logs, group, '"sebs_duration_s"', start_ms)
        if ok == -1:
            print(f"{fn:60s}  (noch keine Logs)")
            continue
        err, first_err = count(logs, group, '?Traceback ?ERROR ?"Task timed out"', start_ms)
        cold, _ = count(logs, group, '"Init Duration"', start_ms)
        print(f"{fn:60s} {ok:4d} {err:7d} {cold:5d}")
        if err:
            problems.append((fn, first_err))

    if problems:
        print("\nErste Fehlermeldung je Funktion:")
        for fn, msg in problems:
            print(f"\n--- {fn}\n{msg[:800]}")
    else:
        print("\nKeine Fehler gefunden.")


if __name__ == "__main__":
    main()