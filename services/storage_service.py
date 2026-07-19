#!/usr/bin/env python3
"""Centralized local storage abstraction for the Flask web app."""

from __future__ import annotations

import mimetypes
import os
import threading
import time
import uuid
from dataclasses import dataclass
from urllib.parse import quote

import cv2
import numpy as np
import requests
from werkzeug.utils import secure_filename

from web_security import decode_image_data_url


@dataclass
class StoredFile:
    filename: str
    absolute_path: str
    relative_url: str
    mime_type: str


class StorageUnavailable(RuntimeError):
    """Raised when production durable storage is required but unavailable."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class StorageService:
    FOLDERS = {
        "captures": "captures",
        "session_images": "session-images",
        "demo_images": "demo-images",
        "history_uploads": "history-uploads",
    }

    def __init__(self, base_dir: str, app_config: dict):
        self.base_dir = os.path.abspath(os.path.expanduser(base_dir))
        self.captures_dir = os.path.join(self.base_dir, "captures")
        self.sessions_dir = os.path.join(self.base_dir, "sessions")
        self.images_dir = os.path.join(self.sessions_dir, "images")
        self.demo_images_dir = os.path.join(self.base_dir, "demo_images")
        self.history_dir = os.path.join(self.base_dir, "history_uploads")
        self.logs_dir = os.path.join(self.base_dir, "logs")
        self.tmp_dir = os.path.join(self.base_dir, "tmp")
        self.max_image_bytes = app_config["MAX_IMAGE_BYTES"]
        self.supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.supabase_service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.storage_bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "lumen-assets").strip()
        self.force_durable = _env_bool("REQUIRE_DURABLE_STORAGE", False)
        self.remote_enabled = bool(self.supabase_url and self.supabase_service_key and self.storage_bucket)
        self._bucket_checked = False
        self._bucket_lock = threading.Lock()
        self.ensure_dirs()

    def ensure_dirs(self):
        for path in [
            self.base_dir,
            self.captures_dir,
            self.sessions_dir,
            self.images_dir,
            self.demo_images_dir,
            self.history_dir,
            self.logs_dir,
            self.tmp_dir,
        ]:
            os.makedirs(path, exist_ok=True)

    def cleanup_temp(self, max_age_seconds: int = 3600):
        now = time.time()
        for filename in os.listdir(self.tmp_dir):
            path = os.path.join(self.tmp_dir, filename)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
            except OSError:
                pass

    def _validated_image_bytes(self, data_url: str):
        image_bytes, mime_type = decode_image_data_url(data_url, self.max_image_bytes)
        nparr = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("Uploaded file is not a valid image")
        return image_bytes, mime_type

    @staticmethod
    def _extension_for_mime(mime_type: str) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }[mime_type]

    def storage_summary(self) -> dict:
        return {
            "backend": "supabase_storage" if self.remote_enabled else "local_filesystem",
            "bucket": self.storage_bucket if self.remote_enabled else None,
            "durable_required": self.force_durable,
            "local_base_dir": self.base_dir,
        }

    def _folder_dir(self, folder: str) -> str:
        return {
            "captures": self.captures_dir,
            "session_images": self.images_dir,
            "demo_images": self.demo_images_dir,
            "history_uploads": self.history_dir,
        }[folder]

    def _route_for(self, folder: str, filename: str) -> str:
        return (
            f"/api/images/{filename}" if folder == "session_images"
            else f"/api/demo-images/{filename}" if folder == "demo_images"
            else f"/api/history-images/{filename}" if folder == "history_uploads"
            else filename
        )

    @staticmethod
    def _safe_relative_name(filename: str | None, fallback_ext: str = "") -> str:
        raw = (filename or f"{uuid.uuid4().hex}{fallback_ext}").replace("\\", "/")
        parts = [secure_filename(part) for part in raw.split("/") if part not in {"", ".", ".."}]
        parts = [part for part in parts if part]
        if not parts:
            parts = [f"{uuid.uuid4().hex}{fallback_ext}"]
        return "/".join(parts)

    def _local_path(self, folder: str, filename: str) -> str:
        safe_name = self._safe_relative_name(filename)
        path = os.path.join(self._folder_dir(folder), safe_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def _object_path(self, folder: str, filename: str) -> str:
        safe_name = self._safe_relative_name(filename)
        return f"{self.FOLDERS[folder]}/{safe_name}"

    def _remote_headers(self, content_type: str | None = None) -> dict:
        headers = {
            "apikey": self.supabase_service_key,
            "Authorization": f"Bearer {self.supabase_service_key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _ensure_remote_ready(self):
        if self.remote_enabled:
            return
        if self.force_durable:
            raise StorageUnavailable(
                "Durable storage is required but Supabase Storage is not configured. "
                "Set SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, and SUPABASE_STORAGE_BUCKET."
            )

    def _ensure_bucket(self):
        if not self.remote_enabled or self._bucket_checked:
            return
        with self._bucket_lock:
            if self._bucket_checked:
                return
            encoded_bucket = quote(self.storage_bucket, safe="")
            bucket_url = f"{self.supabase_url}/storage/v1/bucket/{encoded_bucket}"
            resp = requests.get(bucket_url, headers=self._remote_headers(), timeout=20)
            if resp.status_code == 200:
                self._bucket_checked = True
                return
            if resp.status_code != 404:
                resp.raise_for_status()

            create_url = f"{self.supabase_url}/storage/v1/bucket"
            payload = {
                "id": self.storage_bucket,
                "name": self.storage_bucket,
                "public": False,
                "file_size_limit": self.max_image_bytes,
                "allowed_mime_types": ["image/png", "image/jpeg", "image/webp", "application/json"],
            }
            create_resp = requests.post(
                create_url,
                headers=self._remote_headers("application/json"),
                json=payload,
                timeout=20,
            )
            if create_resp.status_code in (200, 201, 409):
                self._bucket_checked = True
                return
            if create_resp.status_code == 400:
                # Older Storage APIs may reject optional bucket fields; retry minimal.
                minimal = {"id": self.storage_bucket, "name": self.storage_bucket, "public": False}
                create_resp = requests.post(
                    create_url,
                    headers=self._remote_headers("application/json"),
                    json=minimal,
                    timeout=20,
                )
                if create_resp.status_code in (200, 201, 409):
                    self._bucket_checked = True
                    return
            create_resp.raise_for_status()

    def _upload_remote(self, folder: str, filename: str, payload: bytes, mime_type: str):
        self._ensure_remote_ready()
        if not self.remote_enabled:
            return False
        self._ensure_bucket()
        object_path = quote(self._object_path(folder, filename), safe="/")
        bucket = quote(self.storage_bucket, safe="")
        url = f"{self.supabase_url}/storage/v1/object/{bucket}/{object_path}"
        headers = {
            **self._remote_headers(mime_type),
            "x-upsert": "true",
            "cache-control": "3600",
        }
        resp = requests.post(url, headers=headers, data=payload, timeout=30)
        if resp.status_code in (200, 201):
            return True
        if resp.status_code in (400, 405, 409):
            resp = requests.put(url, headers=headers, data=payload, timeout=30)
            if resp.status_code in (200, 201):
                return True
        resp.raise_for_status()
        return True

    def _read_remote(self, folder: str, filename: str):
        if not self.remote_enabled:
            return None
        self._ensure_bucket()
        object_path = quote(self._object_path(folder, filename), safe="/")
        bucket = quote(self.storage_bucket, safe="")
        # JSON files are mutable indexes (for example demo_images.json). Supabase
        # Storage/CDN can otherwise return the previous object immediately after
        # an upsert, making a newly uploaded image briefly appear and disappear.
        cache_buster = f"?cb={time.time_ns()}" if filename.lower().endswith(".json") else ""
        paths = [
            f"{self.supabase_url}/storage/v1/object/authenticated/{bucket}/{object_path}",
            f"{self.supabase_url}/storage/v1/object/{bucket}/{object_path}",
        ]
        last_resp = None
        for url in paths:
            headers = {**self._remote_headers(), "Cache-Control": "no-cache"}
            resp = requests.get(f"{url}{cache_buster}", headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.content, resp.headers.get("Content-Type") or "application/octet-stream"
            if resp.status_code == 404:
                last_resp = resp
                continue
            last_resp = resp
        if last_resp is not None and last_resp.status_code == 404:
            return None
        if last_resp is not None:
            last_resp.raise_for_status()
        return None

    def _read_local(self, folder: str, filename: str):
        path = self._local_path(folder, filename)
        if not os.path.exists(path):
            return None
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            return handle.read(), mime_type

    def _save_local(self, payload: bytes, folder: str, filename: str):
        absolute_path = self._local_path(folder, filename)
        with open(absolute_path, "wb") as handle:
            handle.write(payload)
        return absolute_path

    def save_data_url(
        self,
        data_url: str,
        folder: str,
        filename: str | None = None,
        owner_id: str | None = None,
    ) -> StoredFile:
        image_bytes, mime_type = self._validated_image_bytes(data_url)
        ext = self._extension_for_mime(mime_type)
        safe_name = self._safe_relative_name(filename or f"{uuid.uuid4().hex}{ext}", ext)
        if not safe_name.lower().endswith(ext):
            safe_name = f"{os.path.splitext(safe_name)[0]}{ext}"
        if owner_id:
            safe_name = f"{self._safe_relative_name(owner_id)}/{safe_name}"

        stored_remotely = False
        if self.remote_enabled:
            try:
                stored_remotely = self._upload_remote(folder, safe_name, image_bytes, mime_type)
            except Exception:
                if self.force_durable:
                    raise
        elif self.force_durable:
            self._ensure_remote_ready()

        absolute_path = (
            f"supabase://{self.storage_bucket}/{self._object_path(folder, safe_name)}"
            if stored_remotely
            else self._save_local(image_bytes, folder, safe_name)
        )
        return StoredFile(
            filename=safe_name,
            absolute_path=absolute_path,
            relative_url=self._route_for(folder, safe_name),
            mime_type=mime_type,
        )

    def save_bytes(
        self,
        payload: bytes,
        folder: str,
        filename: str,
        mime_type: str = "application/octet-stream",
        owner_id: str | None = None,
    ):
        safe_name = self._safe_relative_name(filename)
        if owner_id:
            safe_name = f"{self._safe_relative_name(owner_id)}/{safe_name}"
        stored_remotely = False
        if self.remote_enabled:
            try:
                stored_remotely = self._upload_remote(folder, safe_name, payload, mime_type)
            except Exception:
                if self.force_durable:
                    raise
        elif self.force_durable:
            self._ensure_remote_ready()
        if stored_remotely:
            return f"supabase://{self.storage_bucket}/{self._object_path(folder, safe_name)}"
        return self._save_local(payload, folder, safe_name)

    def read_file(self, folder: str, filename: str | None):
        if not filename:
            return None
        remote_result = None
        if self.remote_enabled:
            try:
                remote_result = self._read_remote(folder, filename)
            except Exception:
                if self.force_durable:
                    raise
        if remote_result is not None:
            return remote_result

        local_result = self._read_local(folder, filename)
        if local_result is not None and self.remote_enabled:
            payload, mime_type = local_result
            try:
                self._upload_remote(folder, filename, payload, mime_type)
            except Exception:
                if self.force_durable:
                    raise
        return local_result

    def delete_file(self, folder: str, filename: str | None):
        if not filename:
            return
        if self.remote_enabled:
            try:
                self._ensure_bucket()
                bucket = quote(self.storage_bucket, safe="")
                object_path = self._object_path(folder, filename)
                delete_url = f"{self.supabase_url}/storage/v1/object/{bucket}"
                resp = requests.delete(
                    delete_url,
                    headers=self._remote_headers("application/json"),
                    json={"prefixes": [object_path]},
                    timeout=20,
                )
                if resp.status_code not in (200, 204, 404):
                    single_url = f"{delete_url}/{quote(object_path, safe='/')}"
                    resp = requests.delete(single_url, headers=self._remote_headers(), timeout=20)
                    if resp.status_code not in (200, 204, 404):
                        resp.raise_for_status()
            except Exception:
                if self.force_durable:
                    raise
        try:
            os.remove(self._local_path(folder, filename))
        except OSError:
            pass

    def session_json_path(self, session_id: str) -> str:
        return os.path.join(self.sessions_dir, f"{session_id}.json")

    def cleanup_orphaned_session_images(self, expected_filenames: set[str]):
        for filename in os.listdir(self.images_dir):
            if filename not in expected_filenames:
                self.delete_file("session_images", filename)
