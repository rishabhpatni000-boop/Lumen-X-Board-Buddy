#!/usr/bin/env python3
"""Supabase auth and history helpers for VisualAssistCam."""

from __future__ import annotations

import os
from functools import wraps
from urllib.parse import quote

import requests
from flask import abort, redirect, request, session, url_for


ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.getenv("VISUALASSISTCAM_ADMIN_EMAILS", "rishabhpatni000@gmail.com").split(",")
    if email.strip()
}


def configure_supabase(app):
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"

    app.config["SUPABASE_URL"] = os.getenv("SUPABASE_URL", "").rstrip("/")
    app.config["SUPABASE_ANON_KEY"] = os.getenv("SUPABASE_ANON_KEY", "")


def supabase_config():
    return {
        "url": os.getenv("SUPABASE_URL", "").rstrip("/"),
        "anon_key": os.getenv("SUPABASE_ANON_KEY", ""),
    }


def auth_enabled() -> bool:
    cfg = supabase_config()
    return bool(cfg["url"] and cfg["anon_key"])


def current_user():
    return session.get("user")


def is_admin_user(user: dict | None = None) -> bool:
    user = user or current_user() or {}
    return user.get("email", "").strip().lower() in ADMIN_EMAILS


def current_access_token():
    return session.get("supabase_access_token")


def is_authenticated() -> bool:
    return bool(current_user() and current_access_token())


def safe_next_url(raw_next: str | None) -> str:
    if not raw_next:
        return "/app"
    if raw_next.startswith("//"):
        return "/app"
    if not raw_next.startswith("/"):
        return "/app"
    return raw_next


def login_url_for_request() -> str:
    next_url = safe_next_url(request.full_path[:-1] if request.full_path.endswith("?") else request.full_path)
    return f"{url_for('landing')}?next={quote(next_url, safe='/?:=&')}"


def login_required_page(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(login_url_for_request())
        return view(*args, **kwargs)
    return wrapped


def login_required_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(login_url_for_request())
        return view(*args, **kwargs)
    return wrapped


def admin_required_page(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(login_url_for_request())
        if not is_admin_user():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def admin_required_api(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_authenticated():
            return redirect(login_url_for_request())
        if not is_admin_user():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def fetch_supabase_user(access_token: str):
    cfg = supabase_config()
    if not cfg["url"] or not cfg["anon_key"]:
        raise RuntimeError("Supabase is not configured")

    resp = requests.get(
        f"{cfg['url']}/auth/v1/user",
        headers={
            "apikey": cfg["anon_key"],
            "Authorization": f"Bearer {access_token}",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise ValueError("Unable to verify Supabase user session")
    return resp.json()


def sync_user_profile(access_token: str, user: dict):
    cfg = supabase_config()
    if not cfg["url"] or not cfg["anon_key"]:
        return
    payload = {
        "id": user["id"],
        "email": user.get("email", ""),
        "full_name": (user.get("user_metadata") or {}).get("full_name", ""),
        "avatar_url": (user.get("user_metadata") or {}).get("avatar_url", ""),
    }
    resp = requests.post(
        f"{cfg['url']}/rest/v1/users",
        headers={
            **_supabase_rest_headers(access_token),
            "Prefer": "resolution=merge-duplicates,return=representation",
        },
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def store_session_from_token(access_token: str):
    user = fetch_supabase_user(access_token)
    try:
        sync_user_profile(access_token, user)
    except Exception:
        pass
    session["supabase_access_token"] = access_token
    session["user"] = {
        "id": user["id"],
        "email": user.get("email", ""),
        "full_name": (user.get("user_metadata") or {}).get("full_name", ""),
        "avatar_url": (user.get("user_metadata") or {}).get("avatar_url", ""),
        "is_admin": user.get("email", "").strip().lower() in ADMIN_EMAILS,
    }
    return session["user"]


def clear_auth_session():
    session.pop("supabase_access_token", None)
    session.pop("user", None)


def template_auth_context(callback_endpoint: str, next_url: str | None = None):
    cfg = supabase_config()
    return {
        "auth_enabled": bool(cfg["url"] and cfg["anon_key"]),
        "supabase_url": cfg["url"],
        "supabase_anon_key": cfg["anon_key"],
        "auth_callback_url": url_for(callback_endpoint, _external=True),
        "next_url": safe_next_url(next_url),
        "current_user": current_user(),
        "is_admin": is_admin_user(),
    }


def supabase_service_headers():
    cfg = supabase_config()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not cfg["url"] or not service_key:
        raise RuntimeError("Supabase service role key is not configured")
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


def _supabase_rest_headers(access_token: str):
    cfg = supabase_config()
    return {
        "apikey": cfg["anon_key"],
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def insert_history_record(record: dict):
    cfg = supabase_config()
    token = current_access_token()
    user = current_user()
    if not cfg["url"] or not cfg["anon_key"] or not token or not user:
        return None
    payload = {"user_id": user["id"], **record}

    resp = requests.post(
        f"{cfg['url']}/rest/v1/analysis_history",
        headers={
            **_supabase_rest_headers(token),
            "Prefer": "return=representation",
        },
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data[0] if data else None
    return data


def list_history_records(limit: int = 100):
    return list_history_records_paginated(page=1, per_page=limit)


def list_history_records_paginated(page: int = 1, per_page: int = 12,
                                   search: str = "", subject: str = ""):
    cfg = supabase_config()
    token = current_access_token()
    if not cfg["url"] or not cfg["anon_key"] or not token:
        return {"items": [], "page": page, "per_page": per_page, "total": 0}

    start = max(0, (page - 1) * per_page)
    end = start + per_page - 1
    params = [
        ("select", "id,subject,teacher,session_id,board_id,topic,analysis_text,ai_response,ocr_text,board_svg,image_path,created_at"),
        ("order", "created_at.desc"),
        ("offset", str(start)),
        ("limit", str(per_page)),
    ]
    filters = []
    if search.strip():
        term = search.strip().replace(",", " ")
        filters.append(f"topic.ilike.%{term}%")
        filters.append(f"analysis_text.ilike.%{term}%")
        filters.append(f"subject.ilike.%{term}%")
    if subject.strip():
        params.append(("subject", f"eq.{subject.strip()}"))
    if filters:
        params.append(("or", f"({','.join(filters)})"))

    resp = requests.get(
        f"{cfg['url']}/rest/v1/analysis_history",
        headers={**_supabase_rest_headers(token), "Prefer": "count=exact"},
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    content_range = resp.headers.get("Content-Range", "0-0/0")
    try:
        total = int(content_range.split("/")[-1])
    except (TypeError, ValueError):
        total = len(resp.json())
    return {
        "items": resp.json(),
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }


def count_history_records_this_month():
    cfg = supabase_config()
    token = current_access_token()
    if not cfg["url"] or not cfg["anon_key"] or not token:
        return 0

    from datetime import datetime
    now = datetime.utcnow()
    start = datetime(now.year, now.month, 1).isoformat() + "Z"
    if now.month == 12:
        end = datetime(now.year + 1, 1, 1).isoformat() + "Z"
    else:
        end = datetime(now.year, now.month + 1, 1).isoformat() + "Z"

    resp = requests.get(
        f"{cfg['url']}/rest/v1/analysis_history",
        headers={**_supabase_rest_headers(token), "Prefer": "count=exact"},
        params=[
            ("select", "id"),
            ("created_at", f"gte.{start}"),
            ("created_at", f"lt.{end}"),
            ("limit", "1"),
        ],
        timeout=15,
    )
    resp.raise_for_status()
    content_range = resp.headers.get("Content-Range", "0-0/0")
    try:
        return int(content_range.split("/")[-1])
    except (TypeError, ValueError):
        return len(resp.json())
