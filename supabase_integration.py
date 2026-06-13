#!/usr/bin/env python3
"""Supabase auth and history helpers for VisualAssistCam."""

from __future__ import annotations

import os
from functools import wraps
from urllib.parse import quote

import requests
from flask import jsonify, redirect, request, session, url_for


PROTECTED_JSON_PATHS = {
    "/analyze",
    "/chat",
    "/save",
    "/calibrate",
    "/update-board",
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


def store_session_from_token(access_token: str):
    user = fetch_supabase_user(access_token)
    session["supabase_access_token"] = access_token
    session["user"] = {
        "id": user["id"],
        "email": user.get("email", ""),
        "full_name": (user.get("user_metadata") or {}).get("full_name", ""),
        "avatar_url": (user.get("user_metadata") or {}).get("avatar_url", ""),
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
    cfg = supabase_config()
    token = current_access_token()
    if not cfg["url"] or not cfg["anon_key"] or not token:
        return []

    resp = requests.get(
        f"{cfg['url']}/rest/v1/analysis_history",
        headers=_supabase_rest_headers(token),
        params={
            "select": "id,subject,teacher,session_id,board_id,topic,analysis_text,board_svg,created_at",
            "order": "created_at.desc",
            "limit": str(limit),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
