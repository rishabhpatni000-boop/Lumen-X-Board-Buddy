#!/usr/bin/env python3
"""Idempotently copy Lumen's Supabase Storage objects to Neon Object Storage."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from urllib.parse import quote

import boto3
import requests
from botocore.config import Config
from dotenv import load_dotenv


@dataclass(frozen=True)
class StoredObject:
    key: str
    content_type: str
    size: int | None


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def supabase_headers() -> dict[str, str]:
    key = required_env("SUPABASE_SERVICE_ROLE_KEY")
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def list_objects(prefix: str = "") -> list[StoredObject]:
    base_url = required_env("SUPABASE_URL").rstrip("/")
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "lumen-assets").strip()
    entries: list[StoredObject] = []
    offset = 0
    page_size = 100
    while True:
        response = requests.post(
            f"{base_url}/storage/v1/object/list/{quote(bucket, safe='')}",
            headers={**supabase_headers(), "Content-Type": "application/json"},
            json={
                "prefix": prefix,
                "limit": page_size,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
            timeout=30,
        )
        response.raise_for_status()
        page = response.json()
        for item in page:
            name = str(item.get("name") or "").strip("/")
            if not name:
                continue
            key = f"{prefix}/{name}".strip("/")
            metadata = item.get("metadata")
            if not metadata:
                entries.extend(list_objects(key))
                continue
            raw_size = metadata.get("size")
            entries.append(StoredObject(
                key=key,
                content_type=str(metadata.get("mimetype") or "application/octet-stream"),
                size=int(raw_size) if raw_size is not None else None,
            ))
        if len(page) < page_size:
            return entries
        offset += page_size


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=required_env("AWS_ENDPOINT_URL_S3"),
        region_name=required_env("AWS_REGION"),
        aws_access_key_id=required_env("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=required_env("AWS_SECRET_ACCESS_KEY"),
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def migrate(dry_run: bool) -> tuple[int, int]:
    objects = list_objects()
    if dry_run:
        return len(objects), sum(item.size or 0 for item in objects)

    base_url = required_env("SUPABASE_URL").rstrip("/")
    source_bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "lumen-assets").strip()
    target_bucket = os.getenv("NEON_STORAGE_BUCKET", "lumen-assets").strip()
    s3 = s3_client()
    total_bytes = 0
    for item in objects:
        source_url = (
            f"{base_url}/storage/v1/object/authenticated/"
            f"{quote(source_bucket, safe='')}/{quote(item.key, safe='/')}"
        )
        response = requests.get(source_url, headers=supabase_headers(), timeout=60)
        response.raise_for_status()
        body = response.content
        s3.put_object(
            Bucket=target_bucket,
            Key=item.key,
            Body=body,
            ContentType=item.content_type,
        )
        result = s3.head_object(Bucket=target_bucket, Key=item.key)
        if result["ContentLength"] != len(body):
            raise RuntimeError(f"Size verification failed for {item.key}")
        total_bytes += len(body)
    return len(objects), total_bytes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    count, total_bytes = migrate(args.dry_run)
    verb = "Would migrate" if args.dry_run else "Migrated and verified"
    print(f"{verb} {count} object(s), {total_bytes} byte(s) total")


if __name__ == "__main__":
    main()
