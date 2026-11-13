# Changes to Caribou

This folder contains only the changes made to Caribou for the thesis, not Caribou itself
(thesis, Section 4.2).

- Upstream repository: https://github.com/ubc-cirrus-lab/caribou
- Base version: commit `fea32e7` (main branch as of 3 September 2026)

## Apply the changes

From the root of a fresh Caribou checkout:

    git apply --check <REPO>/caribou/caribou_changes.patch
    git apply <REPO>/caribou/caribou_changes.patch

Then copy the two carbon intensity files used in the thesis into Caribou:

    mkdir caribou/data_collector/components/custom_carbon_data
    copy <REPO>\data\carbon\snapshots_2026-02-10_DE-2025-hourly.csv          caribou\data_collector\components\custom_carbon_data\
    copy <REPO>\data\carbon\snapshots_2026-02-10_US-MIDA-PJM-2025-hourly.csv caribou\data_collector\components\custom_carbon_data\

## What the patch changes

| File | Change |
|---|---|
| `caribou/data_collector/components/carbon/carbon_retriever.py` | Loads the hourly carbon intensity of eu-central-1 and us-east-1 from the local CSV files (direct variant) instead of the Electricity Maps API. Each run uses the 7 days before the simulated day (25 to 31 August 2025) and advances the simulated day by one (state in `custom_carbon_data/sim_day_state.json`). Without an API token, regions outside the simulation are skipped. |
| `caribou/data_collector/components/provider/provider_retriever.py` | Without a Google API key, only eu-central-1 and us-east-1 are used, with fixed coordinates (Frankfurt, Ashburn). Parsing of the AWS price list tolerates missing attributes. |
| `caribou/common/models/remote_client/aws_remote_client.py` | Deploys the Lambda functions for arm64 (Graviton): architecture setting, Docker build platform and Lambda Insights extension. |
| `caribou/deployment_solver/deployment_input/components/loaders/workflow_loader.py` | Fix: Caribou stores an empty list instead of a number when no data-transfer measurement is available; the loader then uses the mean of the list or 0. |

None of the changes alters Caribou's decision logic.

## Changes outside the code

- `caribou/common/constants.py`: `DEPLOYMENT_RESOURCES_BUCKET` must be set to an S3 bucket
  name of your own AWS account (bucket names are globally unique).
- The IAM policy documented by Caribou (`docs/iam_policies.md`) covers only the operation of an
  already deployed workflow. Deploying additionally needs permissions to create DynamoDB tables,
  SNS topics and Lambda functions.
- Docker BuildKit adds attestation manifests that AWS Lambda rejects. Set
  `BUILDX_NO_DEFAULT_ATTESTATIONS=1` before deploying.

The SeBS functions wrapped as Caribou workflows are in `caribou_sebs/`, the replay of the
evaluation week in `evaluation/replay_caribou_week.py`.
