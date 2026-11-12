"""
Sets the home_region in .caribou/config.yml of all SeBS workflows.
Only the home_region block is changed, allowed_regions stays unchanged.

Usage (in the folder caribou_sebs):
    poetry run python set_home_region.py eu-central-1
    poetry run python set_home_region.py eu-central-1 --only sebs_graph_bfs
"""
import argparse
import re
from pathlib import Path


def set_home_region(config: Path, region: str) -> str | None:
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if not re.match(r"\s*home_region\s*:", line):
            continue
        indent = len(line) - len(line.lstrip())
        # search the block below home_region until the indentation decreases again
        for j in range(i + 1, len(lines)):
            sub = lines[j]
            if sub.strip() and len(sub) - len(sub.lstrip()) <= indent:
                break
            m = re.match(r'(\s*region\s*:\s*)["\']?([^"\'\s]+)["\']?(.*)', sub.rstrip("\r\n"))
            if m:
                old = m.group(2)
                end = sub[len(sub.rstrip("\r\n")):]
                lines[j] = f'{m.group(1)}"{region}"{m.group(3)}{end}'
                config.write_text("".join(lines), encoding="utf-8")
                return old
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("region", help="new home region, e.g. eu-central-1")
    ap.add_argument("--only", nargs="*", help="only these workflow folders")
    args = ap.parse_args()

    folders = sorted(p for p in Path(".").iterdir() if p.is_dir() and p.name.startswith("sebs_"))
    if args.only:
        folders = [p for p in folders if p.name in args.only]
    for wf in folders:
        cfg = wf / ".caribou" / "config.yml"
        if not cfg.exists():
            print(f"{wf.name:30s} config.yml missing")
            continue
        old = set_home_region(cfg, args.region)
        print(f"{wf.name:30s} " + (f"{old} -> {args.region}" if old else "home_region NOT found"))


if __name__ == "__main__":
    main()
