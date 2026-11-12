# SeBS Functions of EcoLife as Caribou Workflows

The ten SeBS benchmarks used by EcoLife, each wrapped as a Caribou workflow with a single
function (thesis, Sections 4.1.4 and 4.2.1). The computation (`src/sebs_fn/function.py`) is
taken unchanged from SeBS (https://github.com/spcl/serverless-benchmarks). Only the
following was added:

- `app.py`: Caribou scaffolding that calls the SeBS `handler` and returns only measurements
- `src/sebs_fn/storage.py`: SeBS S3 wrapper with the S3 client fixed to the bucket region
  (`SEBS_BUCKET_REGION`), so that data access still works after Caribou moves the function
  to another region
- `.caribou/config.yml`: regions `eu-central-1` and `us-east-1`, home region `us-east-1`,
  priority order carbon, runtime, cost, 3008 MB memory, 600 s timeout (same for all)

## Mapping and inputs

| EcoLife | Workflow | Input | Source |
|---|---|---|---|
| dynamic-100k | sebs_dynamic_html | `random_len = 100000` | EcoLife profile name |
| pagerank-1000k | sebs_graph_pagerank | 1,000,000 nodes, no seed | EcoLife profile name |
| mst-1000k | sebs_graph_mst | 1,000,000 nodes, no seed | EcoLife profile name |
| bfs-1000k | sebs_graph_bfs | 1,000,000 nodes, no seed | EcoLife profile name |
| upload-img | sebs_uploader | `Jammlich_crop.jpg`, 960 px | bench_aws, replacement file |
| thumbnailer | sebs_thumbnailer | `3_aerial-shot-architecture-bridge-2887493.jpg`, 200×200 | bench_aws |
| video | sebs_video_processing | `city.mp4`, `watermark`, 1 s | bench_aws |
| compression | sebs_compression | `acmart-master` | bench_aws |
| image-recog | sebs_image_recognition | `resnet50-19c8e357.pth`, `800px-Porsche_991_silver_IAA.jpg` | bench_aws |
| dna | sebs_dna_visualisation | `bacillus_subtilis.fasta` | bench_aws |

On request, the EcoLife authors referred to their benchmark setup
(https://github.com/LukeJYK/bench_aws, commit `66c4850`, 23 August 2023). Its `event.json`
per function confirms the inputs of six functions (Y. Jiang, personal communication,
7 October 2026).

Notes:

- **upload-img:** `bench_aws` uses the 800 px version of the image. Wikimedia no longer
  serves this size, so the 960 px version is used, as in SeBS since July 2026 (file 14% larger).
- **dynamic-html and graph functions:** The size follows the names in EcoLife's profiling
  data (`100k`, `1000k`). `bench_aws` states other values (10 and 100,000).
- **No seed** for the graph functions, as in EcoLife (`bench_aws/501.graph-pagerank/function.py`).
  Every invocation creates a new random graph of the same size (`Barabasi(size, 10)`), so the
  three graph payloads contain only `"size"`.

## Steps

All commands run in Caribou's Poetry environment.

**1. Upload the input data to S3** (once)

    git clone https://github.com/spcl/serverless-benchmarks-data.git
    poetry run python upload_inputs.py --bucket YOUR-BUCKET --region us-east-1 --data PATH/serverless-benchmarks-data

This creates the bucket if necessary, uploads about 150 MB and writes `payloads_final/` from
the templates in `payloads/`. If a region other than `us-east-1` is used,
`SEBS_BUCKET_REGION` must be changed in all `config.yml` files.

**2. Deploy** (per workflow, Docker must be running)

    cd sebs_dynamic_html
    poetry run caribou deploy

`deploy` deploys only to the `home_region`. To have the function in both regions, set the
home region to `eu-central-1` with `set_home_region.py`, deploy again and switch back to
`us-east-1`. `sync_version.py` changes the workflow version in `app.py` and `config.yml`
at once.

**3. Invoke the workflows so that Caribou collects measurements**

    poetry run python invoke_runs.py --only sebs_dynamic_html
    poetry run python invoke_runs.py            # all, 10 invocations each

**4. Check**

    poetry run python check_logs.py --hours 2

Every successful invocation writes a line such as
`{"status": 200, "benchmark": "dynamic-100k", "sebs_duration_s": ...}` to CloudWatch.
