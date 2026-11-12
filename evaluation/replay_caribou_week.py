"""
replay_caribou_week.py
======================
Replays Caribou's placement decisions for the simulated week (25 to 31 August 2025)
(thesis, Section 4.2.2).

Caribou normally plans a 24-hour schedule once per day and then waits 24 real hours.
This script creates the seven daily plans directly one after another:

  per simulated day
    1. Run the carbon collector (the patch in carbon_retriever.py loads the 7-day
       window before the simulated day and then advances by one day)
    2. Run Caribou's unchanged solver for every workflow with all 24 hours
    3. Store the chosen region per hour and function

Caribou's decision logic is not changed. Only the waiting time of the deployment
manager (24 h) and its token budget are bypassed: the solver plans every hour.

Prerequisites (as for a regular Caribou run, with caribou/caribou_changes.patch applied):
  - workflows deployed in the home_region (us-east-1) and invoked at least 10 times
  - poetry run caribou log_sync               (transfer the invocations to Caribou's tables)
  - poetry run caribou data_collect --collector all   (at least once before)

Usage (from the root of the patched Caribou repository):
  poetry run python <REPO>/evaluation/replay_caribou_week.py --out <REPO>/results/caribou_replay_home_us
  poetry run python <REPO>/evaluation/replay_caribou_week.py --version 2.0.0 --out <REPO>/results/caribou_replay_home_eu

Output (folder --out):
  placements.csv   sim_date, hour_utc, workflow_id, instance, provider, region
  metrics.csv      sim_date, hour_utc, workflow_id, metrics of the chosen plan
  run_info.json    settings of this run
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
    if versions:  # only workflows with this version (part after the last "-")
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
        raise ValueError(f"Unknown algorithm: {workflow_config.deployment_algorithm}")
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
        raise RuntimeError(f"No solver result found for {workflow_id}")
    return json.loads(raw)


def main() -> None:
    ap = argparse.ArgumentParser(description="Caribou replay for the simulated week")
    ap.add_argument("--prefix", default="sebs_", help="only workflows whose name starts with this prefix")
    ap.add_argument("--out", default="caribou_replay", help="output folder")
    ap.add_argument("--version", nargs="+",
                    help="only workflows with this version, e.g. --version 1.0.0")
    ap.add_argument("--seed", type=int, default=42, help="random seed for the solver")
    ap.add_argument("--calculator", default="simple", choices=["simple", "go"],
                    help="Caribou's metrics calculator (default as in the deployment manager: simple)")
    ap.add_argument("--no-reset", action="store_true",
                    help="do not reset sim_day_state.json (otherwise start at day 0 = 25 August)")
    ap.add_argument("--keep-staging", action="store_true",
                    help="do not remove the solver results from Caribou's staging table after the run")
    args = ap.parse_args()

    endpoints = Endpoints()
    workflow_ids = get_workflow_ids(endpoints, args.prefix, args.version)
    if not workflow_ids:
        sys.exit(f"No deployed workflows with prefix '{args.prefix}' found.")
    print(f"Workflows ({len(workflow_ids)}): {', '.join(workflow_ids)}")

    if not args.no_reset:
        reset_sim_day()
        print("Simulated day reset (start: day 0).")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # update the workflow measurements once (as the deployment manager does before each solve)
    workflow_collector = WorkflowCollector()
    for wf in workflow_ids:
        print(f"Collecting workflow data: {wf}")
        workflow_collector.run_on_workflow(wf)

    week_start: date = patched_retriever._SIM_WEEK_START  # pylint: disable=protected-access
    n_days: int = patched_retriever._SIM_WEEK_LENGTH_DAYS  # pylint: disable=protected-access

    placement_rows: list[dict] = []
    metric_rows: list[dict] = []

    for step in range(n_days):
        day_index = read_sim_day_index()
        sim_date = week_start + timedelta(days=day_index)
        print(f"\n=== Simulated day {day_index + 1}/{n_days}: {sim_date.isoformat()} ===")

        # 1. carbon data for this day (the patch then advances by one day)
        CarbonCollector().run()
        if read_sim_day_index() == day_index:
            sys.exit("The simulated day was not advanced. Is the patch in "
                     "carbon_retriever.py applied and are eu-central-1/us-east-1 in CUSTOM_REGION_CSV_MAP?")

        # 2. + 3. solver per workflow, collect the result
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

        # save intermediate results after every day
        write_csv(out_dir / "placements.csv", placement_rows)
        write_csv(out_dir / "metrics.csv", metric_rows)

    if not args.keep_staging:
        client = endpoints.get_deployment_algorithm_workflow_placement_decision_client()
        for wf in workflow_ids:
            client.remove_key(WORKFLOW_PLACEMENT_SOLVER_STAGING_AREA_TABLE, wf)
        print("\nSolver results removed from Caribou's staging table (no actual migration).")

    with open(out_dir / "run_info.json", "w", encoding="utf-8") as f:
        json.dump({
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "workflows": workflow_ids,
            "week_start": week_start.isoformat(),
            "days": n_days,
            "solve_hours": ALL_HOURS,
            "seed": args.seed,
            "calculator": args.calculator,
            "token_budget": "bypassed (solver plans every hour)",
        }, f, indent=2)

    print(f"\nDone. {len(placement_rows)} placements in {out_dir / 'placements.csv'}")
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
    print("\nHours per region (over the week):")
    for wf, regions in sorted(counts.items()):
        print(f"  {wf:32s} " + ", ".join(f"{reg}: {n}" for reg, n in sorted(regions.items())))


if __name__ == "__main__":
    main()
