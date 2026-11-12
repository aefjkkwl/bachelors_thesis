"""
Uploads the SeBS input data to an S3 bucket and creates the invocation payloads
(payloads_final/*.json) from the templates in payloads/ with the actual bucket name.

Prerequisite:
    git clone https://github.com/spcl/serverless-benchmarks-data.git

Usage (in Caribou's Poetry environment, so that boto3 and the AWS credentials are available):
    poetry run python upload_inputs.py --bucket YOUR-BUCKET --region us-east-1 --data PATH/serverless-benchmarks-data
"""
import argparse
import json
import os

import boto3

# (source in the serverless-benchmarks-data repository, target prefix in the bucket, file pattern)
UPLOADS = [
    ("200.multimedia/210.thumbnailer", "thumbnailer/input", "*.jpg"),
    ("200.multimedia/220.video-processing/city.mp4", "video/input", None),
    ("300.utilities/311.compression/acmart-master", "compression/input/acmart-master", "**"),
    ("400.inference/411.image-recognition/model/resnet50-19c8e357.pth", "image-recognition/model", None),
    ("400.inference/411.image-recognition/fake-resnet/800px-Porsche_991_silver_IAA.jpg", "image-recognition/input", None),
    ("500.scientific/504.dna-visualisation/bacillus_subtilis.fasta", "dna/input", None),
]


def ensure_bucket(s3, bucket, region):
    existing = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    if bucket in existing:
        print(f"Bucket {bucket} already exists.")
        return
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket)
    else:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region})
    print(f"Bucket {bucket} created in {region}.")


def upload_file(s3, path, bucket, key):
    print(f"  {key}  ({os.path.getsize(path) / 1e6:.1f} MB)")
    s3.upload_file(path, bucket, key)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--data", required=True, help="path to the cloned serverless-benchmarks-data repository")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    ensure_bucket(s3, args.bucket, args.region)

    for src, prefix, pattern in UPLOADS:
        full = os.path.join(args.data, src)
        if not os.path.exists(full):
            raise SystemExit(f"Not found: {full}  (is --data correct?)")
        if os.path.isfile(full):
            upload_file(s3, full, args.bucket, f"{prefix}/{os.path.basename(full)}")
        elif pattern == "**":
            for root, _, files in os.walk(full):
                for fn in files:
                    p = os.path.join(root, fn)
                    rel = os.path.relpath(p, full).replace(os.sep, "/")
                    upload_file(s3, p, args.bucket, f"{prefix}/{rel}")
        else:
            ext = pattern.replace("*", "")
            for fn in sorted(os.listdir(full)):
                if fn.endswith(ext):
                    upload_file(s3, os.path.join(full, fn), args.bucket, f"{prefix}/{fn}")

    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "payloads_final")
    os.makedirs(out_dir, exist_ok=True)
    for fn in sorted(os.listdir(os.path.join(here, "payloads"))):
        text = open(os.path.join(here, "payloads", fn), encoding="utf-8").read()
        with open(os.path.join(out_dir, fn), "w", encoding="utf-8") as f:
            f.write(text.replace("BUCKET", args.bucket))
    print(f"\nDone. Payloads with the bucket name are in {out_dir}")
    print("Important: SEBS_BUCKET_REGION in the config.yml files must match --region "
          f"(currently set: us-east-1, given: {args.region}).")


if __name__ == "__main__":
    main()
