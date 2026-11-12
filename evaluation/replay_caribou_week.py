"""
Replay: Caribous Platzierungsentscheidungen fuer die simulierte Woche (25.-31.08.2025).

Caribou plant normalerweise einmal pro Tag einen 24-Stunden-Plan und wartet danach
24 echte Stunden. Dieses Skript erzeugt die 7 Tagesplaene direkt hintereinander:

  pro simuliertem Tag
    1. Carbon-Collector laufen lassen (der Patch in carbon_retriever.py laedt das
       7-Tage-Fenster vor dem simulierten Tag und rueckt danach einen Tag weiter)
    2. Caribous unveraenderten Solver fuer jeden Workflow mit allen 24 Stunden starten
    3. Die gewaehlte Region pro Stunde und Funktion speichern

Caribous Entscheidungslogik wird nicht veraendert. Umgangen werden nur die Wartezeit
des Deployment-Managers (24 h) und sein Token-Budget: Der Solver plant jede Stunde.

Voraussetzungen (wie bei einem normalen Caribou-Durchlauf):
  - Workflows in der home_region (us-east-1) deployt und mind. 10x aufgerufen
  - poetry run caribou log_sync               (Aufrufe in Caribous Tabellen uebertragen)
  - poetry run caribou data_collect --collector all   (mindestens einmal zuvor)
  - Umgebungsvariable ELECTRICITY_MAPS_AUTH_TOKEN gesetzt (wie bei data_collect)

Aufruf im Caribou-Repo (caribou-main):
  poetry run python replay_caribou_week.py
  poetry run python replay_caribou_week.py --prefix sebs_ --out caribou_replay
  poetry run python replay_caribou_week.py --version 1.0.0 --out caribou_replay_home_eu

Ergebnis (Ordner --out):
  placements.csv   sim_date, hour_utc, workflow_id, instance, provider, region
  metrics.csv      sim_date, hour_utc, workflow_id, Kennzahlen des gewaehlten Plans
  run_info.json    Einstellungen dieses Laufs
"""
import argparse
import csv
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from caribou.common.constants import (
    DEFAULT_MONITOR_COOLDOWN,
    DEPLOYMENT_MANAGER_RESOURCE_TABLE,
    WORKFLOW_PLACEMENT_SOLVER_STAGING_AREA_TABLE,
)
from caribou.common.models.endpoints import Endpoints
from caribou.data_collector.components.carbon import carbon_retriever as patched_retriever
from caribou.data_collector.components.carbon.carbon_collector import CarbonCollector
from caribou.data_collector.components.workflow.workflow_collector import WorkflowCollector
from caribou.deployment_solver.workflow_config import WorkflowConfig
from caribou.monitors.deployment_manager import deployment_algorithm_mapping

ALL_HOURS = [str(h) for h in range(24)]


def get_workflow_ids(endpoints: Endpoints, prefix: str, versions: list[str] | None = None) -> list[str]:
    keys = endpoints.get_deployment_manager_client().get_keys(DEPLOYMENT_MANAGER_RESOURCE_TABLE)
    ids = [k for k in keys if k.startswith(prefix)]
    if versions:  # nur Workflows mit dieser Version (Teil nach dem letzten "-")
        ids = [k for k in ids if k.rsplit("-", 1)[-1] in versions]
    return sorted(ids)


def get_workflow_config(endpoints: Endpoints, workflow_id: str) -> WorkflowConfig:
    raw, _ = endpoints.get_data_collector_client().get_value_from_table(
        DEPLOYMENT_MANAGER_RESOURCE_TABLE, workflow_id
    )
    workflow_json = json.loads(raw)
    return WorkflowConfig(json.loads(workflow_json["workflow_config"]))


def read_sim_day_index() -> int:
    state_file = patched_retriever._SIM_STATE_FILE  # pylint: disable=protected-access
    if state_file.exists():
        with open(state_file, encoding="utf-8") as f:
            return json.load(f).get("day_index", 0) % patched_retriever._SIM_WEEK_LENGTH_DAYS
    return 0


def reset_sim_day() -> None:
    state_file = patched_retriever._SIM_STATE_FILE  # pylint: disable=protected-access
    if state_file.exists():
        state_file.unlink()


def run_solver(workflow_config: WorkflowConfig, calculator_type: str) -> None:
    algorithm_class = deployment_algorithm_mapping.get(workflow_config.deployment_algorithm)
    if algorithm_class is None:
        raise ValueError(f"Unbekannter Algorithmus: {workflow_config.deployment_algorithm}")
    algorithm = algorithm_class(
        workflow_config,
        DEFAULT_MONITOR_COOLDOWN,
        deployment_metrics_calculator_type=calculator_type,
        lambda_timeout=False,
    )
    algorithm.run(ALL_HOURS)


def read_solver_result(endpoints: Endpoints, workflow_id: str) -> dict:
    raw, _ = endpoints.get_deployment_algorithm_workflow_placement_decision_client().get_value_from_table(
        WORKFLOW_PLACEMENT_SOLVER_STAGING_AREA_TABLE, workflow_id
    )
    if not raw:
        raise RuntimeError(f"Kein Solver-Ergebnis fuer {workflow_id} gefunden")
    return json.loads(raw)


def main() -> None:
    ap = argparse.ArgumentParser(description="Caribou-Replay fuer die simulierte Woche")
    ap.add_argument("--prefix", default="sebs_", help="nur Workflows mit diesem Namensanfang")
    ap.add_argument("--out", default="caribou_replay", help="Ausgabeordner")
    ap.add_argument("--version", nargs="+",
                    help="nur Workflows mit dieser Version, z. B. --version 1.0.0")
    ap.add_argument("--seed", type=int, default=42, help="Zufallsstartwert fuer den Solver")
    ap.add_argument("--calculator", default="simple", choices=["simple", "go"],
                    help="Caribous Metrics-Calculator (Standard wie Deployment-Manager: simple)")
    ap.add_argument("--no-reset", action="store_true",
                    help="sim_day_state.json nicht zuruecksetzen (sonst Start bei Tag 0 = 25.08.)")
    ap.add_argument("--keep-staging", action="store_true",
                    help="Solver-Ergebnisse nach dem Lauf nicht aus Caribous Staging-Tabelle entfernen")
    args = ap.parse_args()

    endpoints = Endpoints()
    workflow_ids = get_workflow_ids(endpoints, args.prefix, args.version)
    if not workflow_ids:
        sys.exit(f"Keine deployten Workflows mit Praefix '{args.prefix}' gefunden.")
    print(f"Workflows ({len(workflow_ids)}): {', '.join(workflow_ids)}")

    if not args.no_reset:
        reset_sim_day()
        print("Simulierter Tag zurueckgesetzt (Start: Tag 0).")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Messdaten der Workflows einmal aktualisieren (wie im Deployment-Manager vor jedem Solve)
    workflow_collector = WorkflowCollector()
    for wf in workflow_ids:
        print(f"Workflow-Daten sammeln: {wf}")
        workflow_collector.run_on_workflow(wf)

    week_start: date = patched_retriever._SIM_WEEK_START  # pylint: disable=protected-access
    n_days: int = patched_retriever._SIM_WEEK_LENGTH_DAYS  # pylint: disable=protected-access

    placement_rows: list[dict] = []
    metric_rows: list[dict] = []

    for step in range(n_days):
        day_index = read_sim_day_index()
        sim_date = week_start + timedelta(days=day_index)
        print(f"\n=== Simulierter Tag {day_index + 1}/{n_days}: {sim_date.isoformat()} ===")

        # 1. Carbon-Daten fuer diesen Tag (Patch rueckt danach einen Tag weiter)
        CarbonCollector().run()
        if read_sim_day_index() == day_index:
            sys.exit("Der simulierte Tag wurde nicht weitergeschaltet. Ist der Patch in "
                     "carbon_retriever.py aktiv und sind eu-central-1/us-east-1 in CUSTOM_REGION_CSV_MAP?")

        # 2. + 3. Solver je Workflow, Ergebnis einsammeln
        for wf in workflow_ids:
            random.seed(args.seed + step)
            np.random.seed(args.seed + step)
            print(f"  Solver: {wf}")
            run_solver(get_workflow_config(endpoints, wf), args.calculator)
            result = read_solver_result(endpoints, wf)

            for hour, placement in result["time_keys_to_staging_area_data"].items():
                for instance, info in placement.items():
                    placement_rows.append({
                        "sim_date": sim_date.isoformat(),
                        "hour_utc": int(hour),
                        "workflow_id": wf,
                        "instance": instance,
                        "provider": info["provider_region"]["provider"],
                        "region": info["provider_region"]["region"],
                    })
            for hour, metrics in result.get("deployment_metrics", {}).items():
                metric_rows.append({"sim_date": sim_date.isoformat(), "hour_utc": int(hour),
                                    "workflow_id": wf, **metrics})

        # Zwischenstand nach jedem Tag sichern
        write_csv(out_dir / "placements.csv", placement_rows)
        write_csv(out_dir / "metrics.csv", metric_rows)

    if not args.keep_staging:
        client = endpoints.get_deployment_algorithm_workflow_placement_decision_client()
        for wf in workflow_ids:
            client.remove_key(WORKFLOW_PLACEMENT_SOLVER_STAGING_AREA_TABLE, wf)
        print("\nSolver-Ergebnisse aus Caribous Staging-Tabelle entfernt (keine echte Migration).")

    with open(out_dir / "run_info.json", "w", encoding="utf-8") as f:
        json.dump({
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "workflows": workflow_ids,
            "week_start": week_start.isoformat(),
            "days": n_days,
            "solve_hours": ALL_HOURS,
            "seed": args.seed,
            "calculator": args.calculator,
            "token_budget": "umgangen (Solver plant jede Stunde)",
        }, f, indent=2)

    print(f"\nFertig. {len(placement_rows)} Platzierungen in {out_dir / 'placements.csv'}")
    summarize(placement_rows)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict]) -> None:
    counts: dict[str, dict[str, int]] = {}
    for r in rows:
        counts.setdefault(r["workflow_id"], {}).setdefault(r["region"], 0)
        counts[r["workflow_id"]][r["region"]] += 1
    print("\nStunden pro Region (ueber die Woche):")
    for wf, regions in sorted(counts.items()):
        print(f"  {wf:32s} " + ", ".join(f"{reg}: {n}" for reg, n in sorted(regions.items())))


if __name__ == "__main__":
    main()
