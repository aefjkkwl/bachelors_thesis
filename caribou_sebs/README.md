# SeBS-Funktionen aus EcoLife als Caribou-Workflows

Die 10 SeBS-Benchmarks, die EcoLife verwendet, jeweils als eigener Caribou-Workflow mit einer Funktion. Die Rechenlogik (`src/sebs_fn/function.py`) ist unverändert aus SeBS (github.com/spcl/serverless-benchmarks) übernommen. Ergänzt sind nur:

- `app.py`: Caribou-Gerüst, ruft den SeBS-`handler` auf und gibt nur Messwerte zurück
- `src/sebs_fn/storage.py`: SeBS-S3-Anbindung, S3-Client fest in der Bucket-Region
  (`SEBS_BUCKET_REGION`), damit der Zugriff auch nach einer Regionsverschiebung klappt
- `.caribou/config.yml`: Regionen `eu-central-1` + `us-east-1`, Heimatregion `us-east-1`,
  Priorität carbon → runtime → cost, 3008 MB Speicher, 600 s Timeout (für alle gleich)

## Zuordnung und Eingaben

| EcoLife | Workflow | Eingabe | Beleg |
|---|---|---|---|
| dynamic-100k | sebs_dynamic_html | `random_len = 100000` | aus Namen |
| pagerank-1000k | sebs_graph_pagerank | 1.000.000 Knoten, ohne Seed | aus Namen |
| mst-1000k | sebs_graph_mst | 1.000.000 Knoten, ohne Seed | aus Namen |
| bfs-1000k | sebs_graph_bfs | 1.000.000 Knoten, ohne Seed | aus Namen |
| upload-img | sebs_uploader | `Jammlich_crop.jpg`, 960px | bestätigt (bench_aws), Ersatzdatei |
| thumbnailer | sebs_thumbnailer | `3_aerial-shot-architecture-bridge-2887493.jpg`, 200×200 | bestätigt (bench_aws) |
| video | sebs_video_processing | `city.mp4`, `watermark`, 1 s | bestätigt (bench_aws) |
| compression | sebs_compression | `acmart-master` | bestätigt (bench_aws) |
| image-recog | sebs_image_recognition | `resnet50-19c8e357.pth`, `800px-Porsche_991_silver_IAA.jpg` | bestätigt (bench_aws) |
| dna | sebs_dna_visualisation | `bacillus_subtilis.fasta` | bestätigt (bench_aws) |

Die EcoLife-Autoren haben auf Anfrage auf ihr Benchmark-Setup verwiesen: github.com/LukeJYK/bench_aws (Commit `66c4850`, 23.08.2023). Die `event.json` pro Funktion darin bestätigt die Eingaben von 6 Funktionen (Y. Jiang, persönliche Kommunikation, 07.10.2026).

Hinweise:

- **upload-img:** `bench_aws` verwendet die 800px-Version des Bildes. Wikimedia liefert diese Größe nicht mehr aus, daher die 960px-Version wie in SeBS seit Juli 2026 (Datei 14 % größer).
- **dynamic-html und Graph-Funktionen:** Die Größe folgt den Namen in EcoLifes Messdaten (`100k`, `1000k`). `bench_aws` nennt andere Werte (10 bzw. 100.000), das Repository wurde aber für ein Poster auf der MUG'23 erstellt.
- **Kein Seed** bei den Graph-Funktionen, wie bei EcoLife (`bench_aws/501.graph-pagerank/function.py`). Jeder Aufruf erzeugt einen neuen Zufallsgraphen gleicher Größe (`Barabasi(size, 10)`). Die drei Graph-Payloads enthalten deshalb nur `"size"`.

Ausführliche Begründung aller Eingaben: Dokument „Begründung der Funktionseingaben (SeBS/EcoLife)“.

## Ablauf

Alles im Caribou-Poetry-Umfeld (dort, wo du bisher `poetry run caribou ...` ausführst).
Am einfachsten diesen Ordner in dein Caribou-Repo kopieren, z. B. nach `benchmarks/sebs/`.

**1. Eingabedaten nach S3 laden** (einmalig)

    git clone https://github.com/spcl/serverless-benchmarks-data.git
    poetry run python upload_inputs.py --bucket DEIN-BUCKET --region us-east-1 --data PFAD\serverless-benchmarks-data

Legt den Bucket an (falls nötig), lädt ~150 MB hoch und schreibt `payloads_final/`.
Nimmst du eine andere Region als `us-east-1`, musst du `SEBS_BUCKET_REGION` in allen
`config.yml` anpassen.

**2. Deployen** (pro Workflow; Docker muss laufen)

    cd sebs_dynamic_html
    poetry run caribou deploy

Wie bei deinem bisherigen Setup deployt `deploy` nur in die `home_region`. Damit die
Funktion in beiden Regionen existiert: `home_region` auf `eu-central-1` ändern und
erneut deployen (danach zurück auf `us-east-1`).

Reihenfolge-Empfehlung: zuerst `sebs_dynamic_html` komplett durchziehen (deploy + run),
dann die übrigen. `sebs_image_recognition` ist am größten (PyTorch, Image > 1 GB),
`sebs_video_processing` lädt beim Build ein ffmpeg-Binary (`imageio-ffmpeg`).

**3. Aufrufen, damit Caribou Messwerte sammelt**

    poetry run python invoke_runs.py --only sebs_dynamic_html
    poetry run python invoke_runs.py            # alle, je 10 Aufrufe

**4. Prüfen**

In CloudWatch (Log-Gruppe der Funktion) gibt jeder Aufruf eine Zeile wie
`{"status": 200, "benchmark": "dynamic-100k", "sebs_duration_s": ...}` aus.

## Lokal getestet

Mit simuliertem S3 und Caribou-Platzhalter liefen `dynamic_html`, `thumbnailer`,
`compression` und `video_processing` vollständig durch, inklusive Upload-Skript und
Payload-Pfaden. Die übrigen 6 (Graphen, DNA, Image-Recognition, Uploader) sind
syntaktisch geprüft. Ihre Bibliotheken (igraph, squiggle, torch) konnten lokal nicht
installiert werden, sie werden erst beim Docker-Build auf deinem Rechner getestet.
