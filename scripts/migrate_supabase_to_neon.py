#!/usr/bin/env python3
"""Idempotently copy Lumen application rows from Supabase to Neon."""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterable

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.types.json import Jsonb


TABLE_COLUMNS = {
    "analysis_history": (
        "id", "user_id", "subject", "teacher", "session_id", "board_id",
        "topic", "analysis_text", "ocr_text", "ai_response", "board_svg",
        "image_path", "created_at",
    ),
    "usage_events": ("id", "user_id", "event_type", "metadata", "created_at"),
    "class_sessions": (
        "id", "user_id", "subject", "teacher", "created_at", "locked", "captures",
    ),
    "user_quota_overrides": (
        "user_id", "daily_analyses_limit", "monthly_analyses_limit",
        "daily_upload_limit", "notes", "updated_at",
    ),
    "admin_surveys": (
        "id", "created_by", "target_user_id", "target_email", "title",
        "feature_key", "description", "status", "created_at",
    ),
}


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def fetch_rows(table: str, columns: Iterable[str]) -> list[dict]:
    base_url = required_env("SUPABASE_URL").rstrip("/")
    service_key = required_env("SUPABASE_SERVICE_ROLE_KEY")
    selected = ",".join(columns)
    rows: list[dict] = []
    offset = 0
    page_size = 500
    while True:
        response = requests.get(
            f"{base_url}/rest/v1/{table}",
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
            },
            params={
                "select": selected,
                "order": "created_at.asc" if "created_at" in columns else columns[0],
                "offset": offset,
                "limit": page_size,
            },
            timeout=30,
        )
        response.raise_for_status()
        page = response.json()
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def json_value(column: str, value):
    if column in {"metadata", "captures"}:
        return Jsonb(value if value is not None else ({} if column == "metadata" else []))
    return value


def upsert_rows(cursor, table: str, columns: tuple[str, ...], rows: list[dict]) -> None:
    if not rows:
        return
    conflict_column = "user_id" if table == "user_quota_overrides" else "id"
    update_columns = [column for column in columns if column != conflict_column]
    placeholders = ", ".join(["%s"] * len(columns))
    assignments = ", ".join(f"{column} = excluded.{column}" for column in update_columns)
    statement = (
        f"insert into public.{table} ({', '.join(columns)}) values ({placeholders}) "
        f"on conflict ({conflict_column}) do update set {assignments}"
    )
    for row in rows:
        cursor.execute(statement, [json_value(column, row.get(column)) for column in columns])


def migrate(dry_run: bool) -> dict[str, int]:
    user_columns = ("id", "email", "full_name", "avatar_url", "created_at", "updated_at")
    users = fetch_rows("users", user_columns)
    table_rows = {
        table: fetch_rows(table, columns)
        for table, columns in TABLE_COLUMNS.items()
    }
    counts = {"users": len(users), **{table: len(rows) for table, rows in table_rows.items()}}
    if dry_run:
        return counts

    with psycopg.connect(required_env("DATABASE_URL_UNPOOLED")) as connection:
        with connection.cursor() as cursor:
            for user in users:
                cursor.execute(
                    """
                    insert into public.users (
                        id, legacy_supabase_user_id, email, full_name, avatar_url,
                        created_at, updated_at
                    ) values (%s, %s, %s, %s, %s, %s, %s)
                    on conflict (id) do update set
                        legacy_supabase_user_id = excluded.legacy_supabase_user_id,
                        email = excluded.email,
                        full_name = excluded.full_name,
                        avatar_url = excluded.avatar_url,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user["id"], user["id"], (user.get("email") or "").lower(),
                        user.get("full_name") or "", user.get("avatar_url") or "",
                        user.get("created_at"), user.get("updated_at"),
                    ),
                )

            cursor.execute(
                """
                update public.users as app_user
                set neon_auth_user_id = auth_user.id,
                    updated_at = timezone('utc', now())
                from neon_auth."user" as auth_user
                where lower(app_user.email) = lower(auth_user.email)
                  and app_user.neon_auth_user_id is distinct from auth_user.id
                """
            )

            for table, columns in TABLE_COLUMNS.items():
                upsert_rows(cursor, table, columns, table_rows[table])
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    counts = migrate(args.dry_run)
    prefix = "Would migrate" if args.dry_run else "Migrated"
    for table, count in counts.items():
        print(f"{prefix} {count} row(s): {table}")


if __name__ == "__main__":
    main()
