#!/usr/bin/env python3
"""Per-user quota and usage tracking backed by Supabase REST."""

from __future__ import annotations

import datetime as dt
import os

import requests

from supabase_integration import (
    current_access_token,
    current_user,
    is_admin_user,
    supabase_config,
    supabase_service_headers,
)


class QuotaExceeded(Exception):
    def __init__(self, message: str, quota_snapshot: dict | None = None):
        super().__init__(message)
        self.quota_snapshot = quota_snapshot or {}


class QuotaService:
    ADMIN_UNLIMITED = 10**9

    def __init__(self):
        self.daily_analyses_limit = int(os.getenv("DAILY_ANALYSES_LIMIT", "20"))
        self.monthly_analyses_limit = int(os.getenv("MONTHLY_ANALYSES_LIMIT", "200"))
        self.daily_upload_limit = int(os.getenv("DAILY_UPLOAD_LIMIT", "30"))

    def _headers(self):
        cfg = supabase_config()
        token = current_access_token()
        return {
            "apikey": cfg["anon_key"],
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _best_effort_headers(self):
        try:
            return supabase_service_headers()
        except Exception:
            return self._headers()

    def _base_url(self):
        return f"{supabase_config()['url']}/rest/v1/usage_events"

    def _override_url(self):
        return f"{supabase_config()['url']}/rest/v1/user_quota_overrides"

    @staticmethod
    def _today_range():
        today = dt.datetime.utcnow().date()
        start = dt.datetime.combine(today, dt.time.min).isoformat() + "Z"
        end = dt.datetime.combine(today, dt.time.max).isoformat() + "Z"
        return start, end

    @staticmethod
    def _month_range():
        now = dt.datetime.utcnow()
        start = dt.datetime(now.year, now.month, 1)
        if now.month == 12:
            end = dt.datetime(now.year + 1, 1, 1)
        else:
            end = dt.datetime(now.year, now.month + 1, 1)
        return start.isoformat() + "Z", end.isoformat() + "Z"

    def _count(self, event_type: str, start: str, end: str):
        cfg = supabase_config()
        token = current_access_token()
        if not cfg["url"] or not cfg["anon_key"] or not token or not current_user():
            return 0

        try:
            resp = requests.get(
                self._base_url(),
                headers={**self._best_effort_headers(), "Prefer": "count=exact"},
                params=[
                    ("select", "id"),
                    ("user_id", f"eq.{current_user()['id']}"),
                    ("event_type", f"eq.{event_type}"),
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
        except Exception:
            return 0

    def record_event(self, event_type: str, metadata: dict | None = None):
        cfg = supabase_config()
        token = current_access_token()
        user = current_user()
        if not cfg["url"] or not cfg["anon_key"] or not token or not user:
            return None
        try:
            resp = requests.post(
                self._base_url(),
                headers={**self._best_effort_headers(), "Prefer": "return=representation"},
                json={
                    "user_id": user["id"],
                    "event_type": event_type,
                    "metadata": metadata or {},
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data[0] if isinstance(data, list) and data else data
        except Exception:
            return None

    def _limits(self):
        limits = {
            "daily_analyses": self.daily_analyses_limit,
            "monthly_analyses": self.monthly_analyses_limit,
            "daily_uploads": self.daily_upload_limit,
        }
        cfg = supabase_config()
        token = current_access_token()
        user = current_user()
        if not cfg["url"] or not cfg["anon_key"] or not token or not user:
            return limits
        try:
            resp = requests.get(
                self._override_url(),
                headers=self._headers(),
                params=[
                    ("select", "daily_analyses_limit,monthly_analyses_limit,daily_upload_limit"),
                    ("user_id", f"eq.{user['id']}"),
                    ("limit", "1"),
                ],
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            override = data[0] if data else {}
            if override.get("daily_analyses_limit") is not None:
                limits["daily_analyses"] = override["daily_analyses_limit"]
            if override.get("monthly_analyses_limit") is not None:
                limits["monthly_analyses"] = override["monthly_analyses_limit"]
            if override.get("daily_upload_limit") is not None:
                limits["daily_uploads"] = override["daily_upload_limit"]
        except Exception:
            pass
        return limits

    def quota_snapshot(self):
        if is_admin_user():
            return {
                "daily_analyses": 0,
                "monthly_analyses": 0,
                "daily_uploads": 0,
                "daily_remaining": self.ADMIN_UNLIMITED,
                "monthly_remaining": self.ADMIN_UNLIMITED,
                "upload_remaining": self.ADMIN_UNLIMITED,
                "limits": {
                    "daily_analyses": self.ADMIN_UNLIMITED,
                    "monthly_analyses": self.ADMIN_UNLIMITED,
                    "daily_uploads": self.ADMIN_UNLIMITED,
                },
                "is_admin_unlimited": True,
            }
        today_start, today_end = self._today_range()
        month_start, month_end = self._month_range()
        daily_analyses = self._count("analysis", today_start, today_end)
        monthly_analyses = self._count("analysis", month_start, month_end)
        daily_uploads = self._count("upload", today_start, today_end)
        limits = self._limits()
        return {
            "daily_analyses": daily_analyses,
            "monthly_analyses": monthly_analyses,
            "daily_uploads": daily_uploads,
            "daily_remaining": max(0, limits["daily_analyses"] - daily_analyses),
            "monthly_remaining": max(0, limits["monthly_analyses"] - monthly_analyses),
            "upload_remaining": max(0, limits["daily_uploads"] - daily_uploads),
            "limits": limits,
        }

    def ensure_analysis_available(self):
        snapshot = self.quota_snapshot()
        if snapshot["daily_remaining"] <= 0:
            raise QuotaExceeded("Daily analysis quota reached.", snapshot)
        if snapshot["monthly_remaining"] <= 0:
            raise QuotaExceeded("Monthly analysis quota reached.", snapshot)
        return snapshot

    def ensure_upload_available(self):
        snapshot = self.quota_snapshot()
        if snapshot["upload_remaining"] <= 0:
            raise QuotaExceeded("Daily upload quota reached.", snapshot)
        return snapshot

    def recent_activity(self, limit: int = 8):
        cfg = supabase_config()
        token = current_access_token()
        if not cfg["url"] or not cfg["anon_key"] or not token or not current_user():
            return []
        try:
            resp = requests.get(
                self._base_url(),
                headers=self._best_effort_headers(),
                params=[
                    ("select", "event_type,metadata,created_at"),
                    ("user_id", f"eq.{current_user()['id']}"),
                    ("order", "created_at.desc"),
                    ("limit", str(limit)),
                ],
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return []
