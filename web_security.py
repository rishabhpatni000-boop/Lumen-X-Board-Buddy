#!/usr/bin/env python3
"""Shared security helpers for the Flask-based VisualAssistCam servers."""

import base64
import binascii
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler

from flask import jsonify, request, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix


DEFAULT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
DEFAULT_ANON_RATE_LIMIT = "10 per hour; 50 per day"
DEFAULT_READ_RATE_LIMIT = "120 per hour"
DEFAULT_WRITE_RATE_LIMIT = "30 per hour; 200 per day"
ALLOWED_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def setup_request_logging(app, log_dir: str):
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger(f"{app.name}.security")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        fmt = logging.Formatter("%(message)s")
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "access.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    @app.before_request
    def _log_request_start():
        g._request_started_at = time.time()

    @app.after_request
    def _log_request_end(response):
        duration_ms = int((time.time() - getattr(g, "_request_started_at", time.time())) * 1000)
        limit_value = response.headers.get("X-RateLimit-Limit")
        remaining_value = response.headers.get("X-RateLimit-Remaining")
        request_count = None
        try:
            if limit_value is not None and remaining_value is not None:
                request_count = int(limit_value) - int(remaining_value)
        except ValueError:
            request_count = None
        payload = {
            "ip": get_remote_address() or "unknown",
            "timestamp": int(time.time()),
            "method": request.method,
            "path": request.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
            "request_count": request_count,
            "rate_limit": limit_value,
            "remaining": remaining_value,
            "reset_at": response.headers.get("X-RateLimit-Reset"),
        }
        logger.info(json.dumps(payload, sort_keys=True))
        return response

    return logger


def _json_error(message: str, status_code: int):
    response = jsonify({"error": message})
    response.status_code = status_code
    return response


def configure_app_security(app, log_dir: str):
    trust_proxy_count = _env_int("TRUST_PROXY_COUNT", 0)
    if trust_proxy_count > 0:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=trust_proxy_count,
            x_proto=trust_proxy_count,
            x_host=trust_proxy_count,
        )

    app.config["MAX_IMAGE_BYTES"] = _env_int("MAX_IMAGE_BYTES", DEFAULT_MAX_IMAGE_BYTES)
    app.config["MAX_CONTENT_LENGTH"] = _env_int("MAX_REQUEST_BYTES", DEFAULT_MAX_REQUEST_BYTES)
    app.config["RATELIMIT_HEADERS_ENABLED"] = True

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
        strategy=os.getenv("RATELIMIT_STRATEGY", "fixed-window"),
    )

    anon_api_quota = limiter.shared_limit(
        os.getenv("ANON_API_RATE_LIMIT", DEFAULT_ANON_RATE_LIMIT),
        scope="anonymous-api",
    )
    read_api_limit = limiter.limit(os.getenv("READ_API_RATE_LIMIT", DEFAULT_READ_RATE_LIMIT))
    write_api_limit = limiter.shared_limit(
        os.getenv("WRITE_API_RATE_LIMIT", DEFAULT_WRITE_RATE_LIMIT),
        scope="write-api",
    )

    logger = setup_request_logging(app, log_dir)

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_large_request(_err):
        return _json_error(
            f"Request too large. Maximum request size is {app.config['MAX_CONTENT_LENGTH']} bytes.",
            413,
        )

    @app.errorhandler(429)
    def _handle_rate_limit(err):
        logger.warning(
            json.dumps(
                {
                    "ip": get_remote_address() or "unknown",
                    "timestamp": int(time.time()),
                    "path": request.path,
                    "status": 429,
                    "error": str(err),
                },
                sort_keys=True,
            )
        )
        return _json_error("Rate limit exceeded. Please try again later.", 429)

    @app.errorhandler(Exception)
    def _handle_unexpected_error(err):
        if isinstance(err, HTTPException):
            return err
        logger.exception(
            json.dumps(
                {
                    "ip": get_remote_address() or "unknown",
                    "timestamp": int(time.time()),
                    "path": request.path,
                    "status": 500,
                    "error": str(err),
                },
                sort_keys=True,
            )
        )
        if request.path.startswith("/api/") or request.path in {
            "/analyze",
            "/chat",
            "/save",
            "/calibrate",
            "/update-board",
        }:
            return _json_error("Internal server error", 500)
        return "Internal server error", 500

    return {
        "limiter": limiter,
        "anon_api_quota": anon_api_quota,
        "read_api_limit": read_api_limit,
        "write_api_limit": write_api_limit,
        "logger": logger,
    }


def require_json_payload():
    if not request.is_json:
        return None, _json_error("Content-Type must be application/json", 415)
    data = request.get_json(silent=True)
    if data is None:
        return None, _json_error("Invalid JSON payload", 400)
    return data, None


def decode_image_data_url(data_url: str, max_bytes: int):
    if not data_url or "," not in data_url:
        raise ValueError("No image")

    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Image payload must be base64 encoded")

    mime_type = header.split(";", 1)[0].replace("data:", "").strip().lower()
    if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise ValueError("Unsupported image type")

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 image data") from exc

    if len(image_bytes) > max_bytes:
        raise ValueError(f"Image exceeds the {max_bytes} byte limit")

    return image_bytes, mime_type
