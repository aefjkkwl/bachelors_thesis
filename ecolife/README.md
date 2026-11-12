# Changes to EcoLife

This folder contains only the changes made to EcoLife for the thesis, not EcoLife itself
(thesis, Section 4.3).

- Upstream repository: https://github.com/LukeJYK/sc24_eco-life
- Base version: commit `02a04ec`

## Apply the changes

From the root of a fresh EcoLife checkout:

    git checkout 02a04ec
    git apply --check <REPO>/ecolife/ecolife_changes.patch
    git apply <REPO>/ecolife/ecolife_changes.patch

Then copy the two carbon intensity files used in the thesis into EcoLife's
`carbon_intensity/` folder under the names EcoLife expects:

    copy <REPO>\data\carbon\snapshots_2026-02-10_DE-2025-hourly.csv          carbon_intensity\DE_2025_hourly.csv
    copy <REPO>\data\carbon\snapshots_2026-02-10_US-MIDA-PJM-2025-hourly.csv carbon_intensity\US-MIDA-PJM_2025_hourly.csv

## What the patch changes

| File | Change |
|---|---|
| `utils.py` | `load_carbon_intensity` reads the 2025 Electricity Maps exports (direct carbon intensity) instead of the 2023 data and checks that the full period is available. The evaluation period is 7 days (10,080 minutes); the traces are cut to this length. The trace files are read in sorted order, so that the order of the functions is reproducible. |
| `main.py` | New defaults: region DE, start hour 5664 (25 August 2025, 00:00 UTC), 7 days. The traces are cut to the length of the carbon data, and the PSO optimizer receives the interval without the initial learning window (`interval - window_size`). |

EcoLife's optimization, carbon model and energy profiles are unchanged
(`STlambda = 0.5`, `window_size = 20`, `pso_size = 15`).

## Runs used in the thesis

EcoLife always writes its results to `results/eco_life/carbon.json` and
`results/eco_life/st.json`. After each run, these two files were renamed to the run name.

| Run | Command | Result files |
|---|---|---|
| DE, hardware switching (i3/m5zn) | `python main.py --region=DE --server_pair='["i3","m5zn"]'` | `carbon_DE_PairA.json`, `st_DE_PairA.json` |
| DE, i3 only | `python main.py --region=DE --server_pair='["i3","i3"]'` | `carbon_DE_fix_i3.json`, `st_DE_fix_i3.json` |
| DE, m5zn only | `python main.py --region=DE --server_pair='["m5zn","m5zn"]'` | `carbon_DE_fix_m5zn.json`, `st_DE_fix_m5zn.json` |
| US, hardware switching | `python main.py --region=US-MIDA-PJM --server_pair='["i3","m5zn"]'` | `carbon_US_PairA.json`, `st_US_PairA.json` |
| US, i3 only | `python main.py --region=US-MIDA-PJM --server_pair='["i3","i3"]'` | `carbon_US_fix_i3.json`, `st_US_fix_i3.json` |
| US, m5zn only | `python main.py --region=US-MIDA-PJM --server_pair='["m5zn","m5zn"]'` | `carbon_US_fix_m5zn.json`, `st_US_fix_m5zn.json` |

`evaluation/reconstruct_ecolife.py` reads these files and writes the breakdown in
`results/ecolife/`.
