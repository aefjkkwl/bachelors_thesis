# A Review of Carbon Efficiency Techniques in Function-as-a-Service

Code, data and results of the experimental evaluation in the bachelor's thesis
*A Review of Carbon Efficiency Techniques in Function-as-a-Service* (TU Berlin, 2026).

The evaluation compares two carbon-aware FaaS approaches on a common basis: Caribou
(spatial shifting between regions) and EcoLife (dynamic keep-alive and hardware switching).
Both are evaluated over one week (25 to 31 August 2025) in two regions, eu-central-1 (DE)
and us-east-1 (US-MIDA-PJM), with the same workload, carbon data, hardware profiles and
carbon model.

## Repository structure

```
├── caribou/          changes to Caribou as a patch (see caribou/README.md)
├── caribou_sebs/     the ten SeBS functions wrapped as Caribou workflows (see caribou_sebs/README.md)
├── ecolife/          changes to EcoLife as a patch (see ecolife/README.md)
├── data/carbon/      hourly carbon intensity of the 11 candidate zones (Electricity Maps, 2025)
├── evaluation/       scripts of the evaluation
├── notebooks/        all figures, tables and key numbers of the thesis
└── results/          results of all steps
```

Caribou and EcoLife themselves are not included. They are available from their original
repositories, and the READMEs in `caribou/` and `ecolife/` describe how to apply the changes.

## Steps

The steps follow the order of the evaluation. Steps 1, 5 and 6 only need Python; steps 2 to 4
need the patched Caribou or EcoLife and, for Caribou, an AWS account.

| Step | Script | Thesis | Output |
|---|---|---|---|
| 1. Region pair and evaluation week | `evaluation/compare_zones.py` | 4.1.2 | `results/compare_zones/` |
| 2. Deploy and invoke the SeBS workflows on Caribou | `caribou_sebs/` | 4.2.1 | measurements in Caribou's tables |
| 3. Caribou's hourly placements for the week | `evaluation/replay_caribou_week.py` | 4.2.2 | `results/caribou_replay_home_eu/`, `results/caribou_replay_home_us/` |
| 4. EcoLife runs and breakdown | EcoLife `main.py`, `evaluation/reconstruct_ecolife.py` | 4.3 | `results/ecolife/` |
| 5. Carbon of Baseline and Caribou | `evaluation/evaluate_carbon.py` | 4.1.6, 4.1.7 | `results/baseline_caribou_5minKAT/` |
| 6. Figures, tables and key numbers | `notebooks/all_plots.ipynb` | 6 | figures and tables |

Data transfer and latency (thesis, Section 7.4.2):
`evaluation/transfer_sizes.py` and `evaluation/latency_inputs.py` read Caribou's DynamoDB
tables (`results/transfer_sizes.csv`, `results/latency/`), and
`evaluation/transmission_carbon.py` adds the carbon of data transfer
(`results/transmission_home_us/`).

Every script describes its inputs, outputs and usage at the top of the file.

### Reproducing the figures and tables

All results needed for Chapter 6 are included in `results/`, so the notebook runs without
AWS, Caribou or EcoLife:

    pip install pandas numpy matplotlib jinja2 notebook
    jupyter notebook notebooks/all_plots.ipynb

Set `SAVE = True` in the first cell to write the figures to `results/figures/` and the
tables to `results/tables/`.

### Region pair and evaluation week

    python evaluation/compare_zones.py

Ranks all pairs of the 11 zones in `data/carbon/` by the number of crossings of their
carbon intensity, the contrast between a stable and a fluctuating zone and the data quality.
The selected pair is DE (eu-central-1) and US-MIDA-PJM (us-east-1) in ISO week 2025-W35.
Pairs are ranked on the life-cycle carbon intensity, whereas the evaluation uses the direct
variant (thesis, Section 7.4.1).

## Data sources

- Carbon intensity: Electricity Maps, hourly CSV exports for 2025, downloaded on
  10 February 2026 (`data/carbon/`).
- Invocations: Azure Functions trace 2019 (Shahrad et al., USENIX ATC 2020), in the
  selection used by EcoLife (`selected_trace.zip` in the EcoLife repository).
- Functions and inputs: SeBS (https://github.com/spcl/serverless-benchmarks) and
  serverless-benchmarks-data.
- Energy profiles and embodied carbon of the hardware: EcoLife (`data/avg_data/`,
  `server_info.json` in the EcoLife repository).

   Raw EcoLife outputs (`carbon_*.json.gz`, `st_*.json.gz`) are in `results/ecolife/raw/`.
   `evaluation/reconstruct_ecolife.py --results-dir results/ecolife/raw` reproduces the breakdown in `results/ecolife/`.