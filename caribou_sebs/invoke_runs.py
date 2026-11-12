"""
Invokes the deployed SeBS workflows repeatedly through Caribou so that Caribou
collects enough measurements (runtime, data volumes) for its placement decision.

Several invocations in quick succession are needed: Caribou discards runs with a
cold start ("Init Duration"), so the first invocations usually do not count.

The version of each workflow is read from its .caribou/config.yml
(workflow_version), so it does not have to be the same for all workflows.

Usage (in Caribou's Poetry environment, from this folder):
    poetry run python invoke_runs.py                      # all 10, 10 invocations each
    poetry run python invoke_runs.py --runs 5 --only sebs_dynamic_html sebs_graph_bfs
"""
import argparse
import json
import os
import re
import time

from caribou.endpoint.client import Client


def read_version(workflow_dir: str) -> str:
    config_path = os.path.join(workflow_dir, ".caribou", "config.yml")
    with open(config_path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r'\s*workflow_version:\s*["\']?([^"\'\s]+)', line)
            if m:
                return m.group(1)
    raise SystemExit(f"workflow_version not found in {config_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between invocations")
    ap.add_argument("--only", nargs="*", help="only these workflows (folder names)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    pdir = os.path.join(here, "payloads_final")
    if not os.path.isdir(pdir):
        raise SystemExit("payloads_final/ is missing - run upload_inputs.py first.")

    workflows = sorted(f[:-5] for f in os.listdir(pdir) if f.endswith(".json"))
    if args.only:
        workflows = [w for w in workflows if w in args.only]

    for wf in workflows:
        version = read_version(os.path.join(here, wf))
        payload = open(os.path.join(pdir, wf + ".json"), encoding="utf-8").read()
        payload = json.dumps(json.loads(payload))  # compact, one line
        print(f"\n== {wf}-{version}")
        client = Client(f"{wf}-{version}")
        for i in range(1, args.runs + 1):
            try:
                client.run(payload)
                print(f"  invocation {i}/{args.runs} sent")
            except Exception as e:  # continue with the next invocation
                print(f"  invocation {i}/{args.runs} ERROR: {e}")
            time.sleep(args.pause)


if __name__ == "__main__":
    main()