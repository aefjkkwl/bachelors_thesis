"""
Synchronizes the workflow version across app.py and .caribou/config.yml of ONE
workflow folder, so you only change the version number in one place.

Usage (in the folder that contains the workflow folders, e.g. caribou_sebs):
    poetry run python sync_version.py sebs_image_recognition 0.0.2

Optional:
    --update-scripts   also update WORKFLOW_KEY in decode_summary.py and
                       calculate_carbon.py (in the current folder), if present

This will:
    1. Update `version="X.X.X"` in <workflow>/app.py
    2. Update `workflow_version: "X.X.X"` in <workflow>/.caribou/config.yml
    3. (optional) Update `WORKFLOW_KEY = "<workflow>-X.X.X"` in the helper scripts

After running this, you can immediately do:
    poetry run caribou -p <workflow> deploy
"""

import argparse
import re
import sys
from pathlib import Path


def update_app_py(app_py: Path, new_version: str) -> bool:
    if not app_py.exists():
        print(f"WARNING: {app_py} not found, skipping.")
        return False
    text = app_py.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'(CaribouWorkflow\(name="[^"]+",\s*version=")[^"]+(")',
        rf"\g<1>{new_version}\g<2>",
        text,
    )
    if count == 0:
        print(f"WARNING: No version pattern found in {app_py}.")
        return False
    app_py.write_text(new_text, encoding="utf-8")
    print(f"Updated {app_py}: version -> {new_version}")
    return True


def update_config_yml(config_yml: Path, new_version: str) -> bool:
    if not config_yml.exists():
        print(f"WARNING: {config_yml} not found, skipping.")
        return False
    text = config_yml.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r'(workflow_version:\s*")[^"]+(")',
        rf"\g<1>{new_version}\g<2>",
        text,
    )
    if count == 0:
        print(f"WARNING: No workflow_version pattern found in {config_yml}.")
        return False
    config_yml.write_text(new_text, encoding="utf-8")
    print(f"Updated {config_yml}: workflow_version -> {new_version}")
    return True


def update_python_script_workflow_key(path: Path, workflow_name: str, new_version: str) -> bool:
    if not path.exists():
        print(f"INFO: {path} not found, skipping.")
        return False
    text = path.read_text(encoding="utf-8")
    new_key = f"{workflow_name}-{new_version}"
    new_text, count = re.subn(
        r'(WORKFLOW_KEY\s*=\s*")[^"]+(")',
        rf"\g<1>{new_key}\g<2>",
        text,
    )
    if count == 0:
        print(f"WARNING: No WORKFLOW_KEY pattern found in {path}.")
        return False
    path.write_text(new_text, encoding="utf-8")
    print(f"Updated {path}: WORKFLOW_KEY -> {new_key}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync workflow version in app.py and config.yml")
    ap.add_argument("workflow_dir", help="workflow folder, e.g. sebs_image_recognition")
    ap.add_argument("version", help="new version, e.g. 0.0.2")
    ap.add_argument("--update-scripts", action="store_true",
                    help="also update WORKFLOW_KEY in decode_summary.py / calculate_carbon.py")
    args = ap.parse_args()

    workflow_dir = Path(args.workflow_dir)
    if not workflow_dir.is_dir():
        sys.exit(f"Folder not found: {workflow_dir}")
    workflow_name = workflow_dir.resolve().name

    print(f"Synchronizing {workflow_name} to version '{args.version}'...\n")
    ok_app = update_app_py(workflow_dir / "app.py", args.version)
    ok_cfg = update_config_yml(workflow_dir / ".caribou" / "config.yml", args.version)

    if args.update_scripts:
        update_python_script_workflow_key(Path("decode_summary.py"), workflow_name, args.version)
        update_python_script_workflow_key(Path("calculate_carbon.py"), workflow_name, args.version)

    if not (ok_app and ok_cfg):
        sys.exit("\nNot all files were updated, please check the warnings above.")

    print("\nDone. Next step:")
    print(f"  poetry run caribou -p {args.workflow_dir} deploy")


if __name__ == "__main__":
    main()