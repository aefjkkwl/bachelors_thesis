"""
Ruft die deployten SeBS-Workflows mehrfach ueber Caribou auf, damit Caribou
genug Messwerte (Laufzeit, Datenmengen) fuer seine Platzierungsentscheidung hat.

Mehrere Aufrufe kurz hintereinander sind noetig: Caribou verwirft Laeufe mit
Kaltstart ("Init Duration"), die ersten Aufrufe zaehlen also meist nicht.

Die Version jedes Workflows wird aus seiner .caribou/config.yml gelesen
(workflow_version), muss also nicht fuer alle gleich sein.

Aufruf (im Caribou-Poetry-Umfeld, aus diesem Ordner):
    poetry run python invoke_runs.py                      # alle 10, je 10 Aufrufe
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
    raise SystemExit(f"workflow_version nicht gefunden in {config_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--pause", type=float, default=5.0, help="Sekunden zwischen Aufrufen")
    ap.add_argument("--only", nargs="*", help="nur diese Workflows (Ordnernamen)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    pdir = os.path.join(here, "payloads_final")
    if not os.path.isdir(pdir):
        raise SystemExit("payloads_final/ fehlt - zuerst upload_inputs.py ausfuehren.")

    workflows = sorted(f[:-5] for f in os.listdir(pdir) if f.endswith(".json"))
    if args.only:
        workflows = [w for w in workflows if w in args.only]

    for wf in workflows:
        version = read_version(os.path.join(here, wf))
        payload = open(os.path.join(pdir, wf + ".json"), encoding="utf-8").read()
        payload = json.dumps(json.loads(payload))  # kompakt, eine Zeile
        print(f"\n== {wf}-{version}")
        client = Client(f"{wf}-{version}")
        for i in range(1, args.runs + 1):
            try:
                client.run(payload)
                print(f"  Aufruf {i}/{args.runs} abgeschickt")
            except Exception as e:  # weiter mit dem naechsten Aufruf
                print(f"  Aufruf {i}/{args.runs} FEHLER: {e}")
            time.sleep(args.pause)


if __name__ == "__main__":
    main()