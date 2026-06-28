#!/usr/bin/env python3
"""Centralized local storage abstraction for the Flask web app."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
from werkzeug.utils import secure_filename

from web_security import decode_image_data_url


@dataclass
class StoredFile:
    filename: str
    absolute_path: str
    relative_url: str
    mime_type: str


class StorageService:
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

    def save_data_url(self, data_url: str, folder: str, filename: str | None = None) -> StoredFile:
        image_bytes, mime_type = self._validated_image_bytes(data_url)
        ext = self._extension_for_mime(mime_type)
        safe_name = secure_filename(filename or f"{uuid.uuid4().hex}{ext}") or f"{uuid.uuid4().hex}{ext}"
        if not safe_name.lower().endswith(ext):
            safe_name = f"{os.path.splitext(safe_name)[0]}{ext}"
        target_dir = {
            "captures": self.captures_dir,
            "session_images": self.images_dir,
            "demo_images": self.demo_images_dir,
            "history_uploads": self.history_dir,
        }[folder]
        absolute_path = os.path.join(target_dir, safe_name)
        with open(absolute_path, "wb") as handle:
            handle.write(image_bytes)
        return StoredFile(
            filename=safe_name,
            absolute_path=absolute_path,
            relative_url=(
                f"/api/images/{safe_name}" if folder == "session_images"
                else f"/api/demo-images/{safe_name}" if folder == "demo_images"
                else safe_name
            ),
            mime_type=mime_type,
        )

    def save_bytes(self, image_bytes: bytes, folder: str, filename: str):
        target_dir = {
            "captures": self.captures_dir,
            "session_images": self.images_dir,
            "demo_images": self.demo_images_dir,
            "history_uploads": self.history_dir,
        }[folder]
        absolute_path = os.path.join(target_dir, filename)
        with open(absolute_path, "wb") as handle:
            handle.write(image_bytes)
        return absolute_path

    def delete_file(self, folder: str, filename: str | None):
        if not filename:
            return
        target_dir = {
            "captures": self.captures_dir,
            "session_images": self.images_dir,
            "demo_images": self.demo_images_dir,
            "history_uploads": self.history_dir,
        }[folder]
        try:
            os.remove(os.path.join(target_dir, filename))
        except OSError:
            pass

    def session_json_path(self, session_id: str) -> str:
        return os.path.join(self.sessions_dir, f"{session_id}.json")

    def cleanup_orphaned_session_images(self, expected_filenames: set[str]):
        for filename in os.listdir(self.images_dir):
            if filename not in expected_filenames:
                self.delete_file("session_images", filename)
