#!/usr/bin/env python3
"""
Lumen — three-view whiteboard assistant
Original camera | AI Board (SVG) | AI Analysis (text)
"""

import os
import re
import json
import uuid
import time
import datetime
import hashlib
import numpy as np
import cv2
import threading
import webbrowser
import base64
import requests
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template, redirect, url_for, Response, abort

from supabase_integration import (
    admin_required_api,
    admin_required_page,
    count_history_records_this_month,
    clear_auth_session,
    configure_supabase,
    current_access_token,
    current_user,
    is_admin_user,
    insert_history_record,
    list_history_records_paginated,
    login_required_api,
    login_required_page,
    safe_next_url,
    store_session_from_token,
    supabase_config,
    supabase_service_headers,
    template_auth_context,
)
from services.cache_service import TTLCache, stable_cache_key
from services.logging_service import log_event, log_warning
from services.quota_service import QuotaExceeded, QuotaService
from services.storage_service import StorageService, StorageUnavailable
from web_security import configure_app_security, decode_image_data_url, require_json_payload

# ── Claude ────────────────────────────────────────────────────────────────────
try:
    import anthropic as _asdk
    _claude = _asdk.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    CLAUDE_AVAILABLE = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
except ImportError:
    _claude = None
    CLAUDE_AVAILABLE = False

app = Flask(__name__, template_folder="templates")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True
configure_supabase(app)

@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response

DATA_DIR = os.getenv(
    "VISUALASSISTCAM_DATA_DIR",
    os.path.expanduser("~/Desktop/Lumen_Data"),
)

STORAGE = StorageService(DATA_DIR, {"MAX_IMAGE_BYTES": app.config["MAX_IMAGE_BYTES"]} if "MAX_IMAGE_BYTES" in app.config else {"MAX_IMAGE_BYTES": 5 * 1024 * 1024})
SECURITY = configure_app_security(app, STORAGE.logs_dir)
anon_api_quota = SECURITY["anon_api_quota"]
read_api_limit = SECURITY["read_api_limit"]
write_api_limit = SECURITY["write_api_limit"]
QUOTAS = QuotaService()
ANALYSIS_CACHE = TTLCache(ttl_seconds=int(os.getenv("ANALYSIS_CACHE_TTL", "600")), max_entries=128)
BOARD_CACHE = TTLCache(ttl_seconds=int(os.getenv("BOARD_CACHE_TTL", "600")), max_entries=128)
STORAGE.max_image_bytes = app.config["MAX_IMAGE_BYTES"]
STORAGE.cleanup_temp()

# ── Session helpers ──────────────────────────────────────────────────────────

def _supabase_user_headers():
    cfg = supabase_config()
    token = current_access_token()
    return {
        "apikey": cfg["anon_key"],
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _supabase_persistence_headers():
    try:
        return supabase_service_headers()
    except Exception:
        return _supabase_user_headers()


def _current_user_id_required() -> str:
    user_id = (current_user() or {}).get("id")
    if not user_id:
        raise RuntimeError("No signed-in user is available for persistence")
    return user_id


def _capture_for_store(cap: dict) -> dict:
    return dict(cap)


def _session_payload_for_store(s: dict) -> dict:
    return {
        "id": s["id"],
        "user_id": (current_user() or {}).get("id"),
        "subject": s.get("subject"),
        "teacher": s.get("teacher"),
        "created_at": s.get("created_at"),
        "locked": bool(s.get("locked", False)),
        "captures": [_capture_for_store(cap) for cap in s.get("captures", [])],
    }


def _demo_images_json_path() -> str:
    return os.path.join(STORAGE.demo_images_dir, "demo_images.json")


def _load_demo_images() -> list[dict]:
    rows = None
    stored = STORAGE.read_file("demo_images", "demo_images.json")
    if stored is not None:
        try:
            rows = json.loads(stored[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            rows = []
    try:
        if rows is None:
            with open(_demo_images_json_path(), "r", encoding="utf-8") as handle:
                rows = json.load(handle)
    except (OSError, json.JSONDecodeError):
        rows = []
    if not isinstance(rows, list):
        return []
    cleaned = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("filename"):
            continue
        cleaned.append({
            "id": str(row.get("id") or uuid.uuid4().hex),
            "title": str(row.get("title") or "Demo image"),
            "caption": str(row.get("caption") or ""),
            "subject": str(row.get("subject") or "All"),
            "filename": str(row.get("filename")),
            "active": bool(row.get("active", True)),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or row.get("created_at") or ""),
            "created_by": str(row.get("created_by") or ""),
        })
    return cleaned


def _save_demo_images(rows: list[dict]):
    payload = json.dumps(rows, indent=2, sort_keys=True).encode("utf-8")
    STORAGE.save_bytes(payload, "demo_images", "demo_images.json", "application/json")


def _demo_image_payload(row: dict) -> dict:
    return {
        **row,
        "url": f"/api/demo-images/{row['filename']}",
    }


def _use_supabase_session_store() -> bool:
    cfg = supabase_config()
    return bool(cfg["url"] and cfg["anon_key"] and current_user())


def _allow_local_session_fallback() -> bool:
    raw = os.getenv("ALLOW_LOCAL_SESSION_FALLBACK", "").strip().lower()
    if raw:
        return raw in {"1", "true", "yes", "on"}
    return not STORAGE.force_durable


def _session_store_fallback_allowed(error: Exception) -> bool:
    if not _allow_local_session_fallback():
        return False
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return error.response.status_code in (401, 403, 404)
    return False


def _storage_user_id() -> str | None:
    return (current_user() or {}).get("id")


def _is_user_scoped_file_allowed(filename: str) -> bool:
    if is_admin_user():
        return True
    user_id = _storage_user_id()
    if not user_id:
        return False
    parts = filename.replace("\\", "/").split("/")
    # Older local records were filename-only. Keep them readable while future
    # records are stored under the owning user's id.
    return len(parts) == 1 or parts[0] == user_id


def _serve_stored_file(folder: str, filename: str, user_scoped: bool = False):
    if user_scoped and not _is_user_scoped_file_allowed(filename):
        abort(404)
    try:
        stored = STORAGE.read_file(folder, filename)
    except StorageUnavailable as e:
        return jsonify({"error": str(e)}), 503
    if stored is None:
        abort(404)
    payload, mime_type = stored
    return Response(payload, mimetype=mime_type)


def _delete_stored_file_best_effort(folder: str, filename: str | None, **context):
    if not filename:
        return
    try:
        STORAGE.delete_file(folder, filename)
    except Exception as e:
        log_warning(
            SECURITY["logger"],
            "stored_file_delete_failed",
            folder=folder,
            filename=filename,
            error=str(e),
            **context,
        )

def _sessions_list() -> list:
    if _use_supabase_session_store():
        try:
            cfg = supabase_config()
            user_id = _current_user_id_required()
            resp = requests.get(
                f"{cfg['url']}/rest/v1/class_sessions",
                headers=_supabase_persistence_headers(),
                params=[
                    ("select", "id,subject,teacher,created_at,locked,captures"),
                    ("user_id", f"eq.{user_id}"),
                    ("order", "created_at.desc"),
                ],
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if not _session_store_fallback_allowed(e):
                raise
    out = []
    for f in os.listdir(STORAGE.sessions_dir):
        if f.endswith(".json"):
            try:
                with open(os.path.join(STORAGE.sessions_dir, f)) as fp:
                    out.append(json.load(fp))
            except Exception:
                pass
    return out

def _session_get(sid: str):
    if _use_supabase_session_store():
        try:
            cfg = supabase_config()
            user_id = _current_user_id_required()
            resp = requests.get(
                f"{cfg['url']}/rest/v1/class_sessions",
                headers=_supabase_persistence_headers(),
                params=[
                    ("select", "id,subject,teacher,created_at,locked,captures"),
                    ("id", f"eq.{sid}"),
                    ("user_id", f"eq.{user_id}"),
                    ("limit", "1"),
                ],
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            return data[0] if data else None
        except Exception as e:
            if not _session_store_fallback_allowed(e):
                raise
    p = STORAGE.session_json_path(sid)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None

def _session_save(s: dict):
    if _use_supabase_session_store():
        try:
            cfg = supabase_config()
            payload = _session_payload_for_store(s)
            resp = requests.post(
                f"{cfg['url']}/rest/v1/class_sessions",
                headers={**_supabase_persistence_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
                json=payload,
                timeout=20,
            )
            resp.raise_for_status()
            return
        except Exception as e:
            if not _session_store_fallback_allowed(e):
                raise
    with open(STORAGE.session_json_path(s["id"]), "w") as f:
        json.dump(s, f, indent=2)

PORT = 5050
_pool = ThreadPoolExecutor(max_workers=2)


def landing_founder_photo_url():
    for filename in ("rishabh-founder.jpg", "rishabh-founder.jpeg", "rishabh-founder.png", "rishabh-founder.webp"):
        photo_path = os.path.join(app.root_path, "static", "images", filename)
        if os.path.exists(photo_path):
            return url_for("static", filename=f"images/{filename}")
    return None

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def landing():
    user = current_user()
    next_url = safe_next_url(request.args.get("next"))
    return render_template(
        "landing.html",
        claude_available=CLAUDE_AVAILABLE,
        founder_photo_url=landing_founder_photo_url(),
        landing_logged_in=bool(user),
        landing_user=user,
        **template_auth_context("auth_callback", next_url=next_url),
    )


@app.route("/auth/callback")
def auth_callback():
    return render_template("auth_callback.html", **template_auth_context("auth_callback"))


@app.route("/auth/session", methods=["POST"])
def auth_session():
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    access_token = data.get("access_token", "").strip()
    next_url = safe_next_url(data.get("next"))
    if not access_token:
        return jsonify({"error": "Missing access token"}), 400
    try:
        user = store_session_from_token(access_token)
        return jsonify({"ok": True, "redirect_to": next_url, "user": user})
    except Exception as e:
        return jsonify({"error": str(e)}), 401


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    clear_auth_session()
    return jsonify({"ok": True})


@app.route("/app")
@login_required_page
def app_dashboard():
    return render_template(
        "index.html",
        claude_available=CLAUDE_AVAILABLE,
        **template_auth_context("auth_callback"),
    )


@app.route("/camera")
@login_required_page
def camera_dashboard():
    return redirect(url_for("app_dashboard"))


@app.route("/history")
@login_required_page
def history_page():
    return render_template(
        "history.html",
        claude_available=CLAUDE_AVAILABLE,
        **template_auth_context("auth_callback"),
    )


@app.route("/settings")
@login_required_page
def settings_page():
    return render_template(
        "settings.html",
        claude_available=CLAUDE_AVAILABLE,
        quota_snapshot=QUOTAS.quota_snapshot(),
        **template_auth_context("auth_callback"),
    )


@app.route("/admin")
@admin_required_page
def admin_dashboard():
    return render_template(
        "admin.html",
        claude_available=CLAUDE_AVAILABLE,
        **template_auth_context("auth_callback"),
    )


@app.route("/analyze", methods=["POST"])
@anon_api_quota
@login_required_api
def analyze():
    try:
        data, error_response = require_json_payload()
        if error_response:
            return error_response
        try:
            quota_snapshot = QUOTAS.ensure_analysis_available()
        except QuotaExceeded as e:
            return jsonify({"svg": "", "analysis": "", "error": str(e), "quota": e.quota_snapshot}), 429
        try:
            image_bytes, _ = decode_image_data_url(data.get("image", ""),
                                                   app.config["MAX_IMAGE_BYTES"])
        except ValueError as e:
            return jsonify({"svg": "", "analysis": "", "error": str(e)}), 400

        if not CLAUDE_AVAILABLE:
            return jsonify({"svg": "", "analysis": "Claude API key not configured.",
                            "error": "No API key"})

        # Accept custom prompts from frontend (user-editable via Settings)
        custom_svg_prompt      = data.get("svg_prompt", None)
        custom_analysis_prompt = data.get("analysis_prompt", None)

        cache_key = stable_cache_key("analyze", {
            "image_sha": hashlib.sha256(image_bytes).hexdigest(),
            "svg_prompt": custom_svg_prompt or "",
            "analysis_prompt": custom_analysis_prompt or "",
        })
        cached = ANALYSIS_CACHE.get(cache_key)
        if cached is not None:
            return jsonify({**cached, "quota": quota_snapshot})

        include_svg = bool((custom_svg_prompt or "").strip())

        svg_f = _pool.submit(_gen_svg, image_bytes, custom_svg_prompt) if include_svg else None
        analysis_f = _pool.submit(_gen_analysis, image_bytes, custom_analysis_prompt)

        svg_result = svg_f.result() if svg_f else {"svg": "", "error": ""}
        analysis_result = analysis_f.result()
        result = {
            "svg": svg_result["svg"],
            "analysis": analysis_result["analysis"],
        }
        if svg_result.get("error"):
            result["svg_error"] = svg_result["error"]
        if analysis_result.get("error"):
            result["error"] = analysis_result["error"]
        ANALYSIS_CACHE.set(cache_key, result)
        QUOTAS.record_event("analysis", {"session_id": data.get("session_id", ""), "board_id": data.get("board_id")})
        QUOTAS.record_event("ai_request", {"route": "/analyze"})
        QUOTAS.record_event("ocr_request", {"route": "/analyze"})
        log_event(
            SECURITY["logger"],
            "analysis_completed",
            ip=request.remote_addr,
            user_id=(current_user() or {}).get("id"),
        )
        return jsonify({**result, "quota": QUOTAS.quota_snapshot()})
    except Exception as e:
        print(f"/analyze route error: {e}")
        return jsonify({"svg": "", "analysis": "", "error": f"Internal server error during analysis: {e}"}), 500


@app.route("/chat", methods=["POST"])
@anon_api_quota
@login_required_api
def chat():
    """Continue the lesson conversation with Claude Teacher."""
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    message = data.get("message", "").strip()
    history = data.get("history", [])   # [{role,content}, ...]
    context = data.get("context", "")   # initial board analysis
    if not message:
        return jsonify({"response": "No message"})
    if not CLAUDE_AVAILABLE:
        return jsonify({"response": "Claude API key not configured."})
    try:
        system = (
            "You are a patient, encouraging teacher helping a student understand "
            "their classroom lesson. Keep answers clear, friendly, and concise. "
            "Use simple examples where helpful. "
            "At the end of longer answers, suggest one follow-up question "
            "the student might want to ask next.\n\n"
            "IMPORTANT FORMATTING RULES — the student reads your reply as plain text:\n"
            "- Never use LaTeX or dollar signs for maths (no $, $$, \\frac, \\sqrt etc.).\n"
            "  Write maths in plain English instead: 'a squared + b squared = c squared'\n"
            "  or use simple symbols: a² + b² = c², √25 = 5\n"
            "- Never use Markdown: no **, __, ##, *italics*, or --- dividers.\n"
            "- Write in plain sentences and short paragraphs only.\n"
        )
        if context:
            system += f"The whiteboard lesson context:\n{context}"

        resp = _claude.messages.create(
            model="claude-opus-4-8",
            max_tokens=512,
            system=system,
            messages=history + [{"role": "user", "content": message}],
        )
        QUOTAS.record_event("ai_request", {"route": "/chat"})
        return jsonify({"response": resp.content[0].text.strip()})
    except Exception as e:
        print(f"Chat error: {e}")
        return jsonify({"response": f"Error: {e}"})


@app.route("/save", methods=["POST"])
@anon_api_quota
@login_required_api
def save():
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    img_data = data.get("image", "")
    try:
        QUOTAS.ensure_upload_available()
        stored = STORAGE.save_data_url(
            img_data,
            "captures",
            data.get("filename", "capture.png"),
            owner_id=_storage_user_id(),
        )
        QUOTAS.record_event("upload", {"route": "/save", "filename": stored.filename})
        log_event(SECURITY["logger"], "upload_saved", ip=request.remote_addr, filename=stored.filename)
        return jsonify({"ok": True, "path": stored.absolute_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Whiteboard calibration (auto-frame + perspective correction) ──────────────

@app.route("/calibrate", methods=["POST"])
@anon_api_quota
@login_required_api
def calibrate():
    """Detect whiteboard in frame. Returns corners + homography for perspective correction."""
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    try:
        image_bytes, _ = decode_image_data_url(data.get("image", ""), app.config["MAX_IMAGE_BYTES"])
        nparr = np.frombuffer(image_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is None:
            return jsonify({"success": False, "error": "Could not decode image"})
        h, w = frame.shape[:2]

        corners = _find_whiteboard(frame)
        if corners is None:
            return jsonify({
                "success": False,
                "message": (
                    "Could not detect whiteboard edges clearly. "
                    "Ensure the board is well-lit and its edges are visible. "
                    "Auto-zoom will still be applied using the full frame."
                ),
                "frame_size": [w, h],
                # Fall back: use full frame as crop
                "corners": [[0,0],[w,0],[w,h],[0,h]],
                "homography": np.eye(3).tolist(),
                "output_size": [w, h],
            })

        ordered = _order_corners(corners)
        tl, tr, br, bl = ordered

        # Output dimensions — keep the natural aspect ratio of the board
        out_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
        out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))

        src_pts = ordered.astype(np.float32)
        dst_pts = np.array([[0, 0], [out_w, 0], [out_w, out_h], [0, out_h]],
                           dtype=np.float32)
        H, _ = cv2.findHomography(src_pts, dst_pts)

        return jsonify({
            "success":     True,
            "corners":     ordered.tolist(),
            "homography":  H.tolist(),
            "output_size": [out_w, out_h],
            "frame_size":  [w, h],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _find_whiteboard(frame: np.ndarray):
    """Detect the largest rectangular region (the whiteboard) in the frame."""
    h, w = frame.shape[:2]
    min_area = 0.05 * h * w        # at least 5% of the frame
    max_area = 0.98 * h * w        # at most 98% (not the entire frame)

    gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    best = None
    best_area = 0

    # Try multiple Canny thresholds to handle different lighting conditions
    for lo, hi in [(30, 100), (20, 80), (50, 150), (10, 60)]:
        edges   = cv2.Canny(blurred, lo, hi)
        kernel  = np.ones((3, 3), np.uint8)
        dilated = cv2.dilate(edges, kernel, iterations=3)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL,
                                        cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        for cnt in contours[:8]:
            area = cv2.contourArea(cnt)
            if area < min_area or area > max_area:
                continue
            peri  = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
            if len(approx) == 4 and area > best_area:
                best      = approx.reshape(4, 2).astype(np.float32)
                best_area = area

        if best is not None:
            break

    return best


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Return corners in order: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype=np.float32)
    s         = pts.sum(axis=1)
    diff      = np.diff(pts, axis=1).flatten()
    rect[0]   = pts[np.argmin(s)]     # top-left     (min x+y)
    rect[2]   = pts[np.argmax(s)]     # bottom-right (max x+y)
    rect[1]   = pts[np.argmin(diff)]  # top-right    (min y-x)
    rect[3]   = pts[np.argmax(diff)]  # bottom-left  (max y-x)
    return rect


# ── Board update endpoint (incremental + first-time) ─────────────────────────

@app.route("/update-board", methods=["POST"])
@anon_api_quota
@login_required_api
def update_board():
    """Update the AI Board. First call generates fresh; subsequent calls update incrementally."""
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    img_data     = data.get("image", "")
    previous_svg = data.get("previous_svg", "")
    svg_prompt   = data.get("svg_prompt", None)

    if not img_data:
        return jsonify({"svg": "", "is_new_board": False, "error": "No image"})
    if not CLAUDE_AVAILABLE:
        return jsonify({"svg": "", "is_new_board": False, "error": "No API key"})
    try:
        image_bytes, _ = decode_image_data_url(img_data, app.config["MAX_IMAGE_BYTES"])
    except ValueError as e:
        return jsonify({"svg": "", "is_new_board": False, "error": str(e)})

    cache_key = stable_cache_key("board", {
        "image_sha": hashlib.sha256(image_bytes).hexdigest(),
        "previous_svg": previous_svg[:500],
        "svg_prompt": svg_prompt or "",
    })
    cached = BOARD_CACHE.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    if previous_svg.strip().startswith("<svg"):
        result = _gen_svg_incremental(image_bytes, previous_svg, svg_prompt)
    else:
        svg_result = _gen_svg(image_bytes, svg_prompt)
        result = {"svg": svg_result["svg"], "is_new_board": False, "error": svg_result["error"]}
    BOARD_CACHE.set(cache_key, result)
    QUOTAS.record_event("ai_request", {"route": "/update-board"})
    return jsonify(result)


# ── Session API ──────────────────────────────────────────────────────────────

@app.route("/api/sessions", methods=["GET"])
@read_api_limit
@login_required_api
def api_sessions():
    try:
        return jsonify(_sessions_list())
    except Exception as e:
        log_warning(SECURITY["logger"], "session_list_failed", error=str(e))
        return jsonify({"error": "Could not load class sessions", "details": str(e)}), 500


@app.route("/api/sessions", methods=["POST"])
@write_api_limit
@login_required_api
def api_create_session():
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    s = {
        "id":         str(uuid.uuid4())[:12],
        "subject":    data.get("subject", "Unknown").strip(),
        "teacher":    data.get("teacher", "").strip(),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "locked":     False,
        "captures":   [],
    }
    try:
        _session_save(s)
        return jsonify(s)
    except Exception as e:
        log_warning(SECURITY["logger"], "session_create_failed", error=str(e))
        return jsonify({"error": "Could not save class session", "details": str(e)}), 500


@app.route("/api/sessions/<sid>", methods=["GET"])
@read_api_limit
@login_required_api
def api_get_session(sid):
    try:
        s = _session_get(sid)
        return jsonify(s) if s else (jsonify({"error": "Not found"}), 404)
    except Exception as e:
        log_warning(SECURITY["logger"], "session_get_failed", session_id=sid, error=str(e))
        return jsonify({"error": "Could not load class session", "details": str(e)}), 500


@app.route("/api/sessions/<sid>/lock", methods=["POST"])
@write_api_limit
@login_required_api
def api_toggle_lock(sid):
    try:
        s = _session_get(sid)
    except Exception as e:
        log_warning(SECURITY["logger"], "session_lock_load_failed", session_id=sid, error=str(e))
        return jsonify({"error": "Could not load class session", "details": str(e)}), 500
    if not s:
        return jsonify({"error": "Not found"}), 404
    s["locked"] = not s.get("locked", False)
    try:
        _session_save(s)
        return jsonify({"locked": s["locked"]})
    except Exception as e:
        log_warning(SECURITY["logger"], "session_lock_save_failed", session_id=sid, error=str(e))
        return jsonify({"error": "Could not update class session", "details": str(e)}), 500


@app.route("/api/sessions/<sid>/capture", methods=["POST"])
@anon_api_quota
@login_required_api
def api_add_capture(sid):
    try:
        s = _session_get(sid)
    except Exception as e:
        log_warning(SECURITY["logger"], "session_capture_load_failed", session_id=sid, error=str(e))
        return jsonify({"error": "Could not load class session", "details": str(e)}), 500
    if not s:
        return jsonify({"error": "Not found"}), 404
    if s.get("locked"):
        return jsonify({"error": "Session is locked"}), 403

    data, error_response = require_json_payload()
    if error_response:
        return error_response
    try:
        QUOTAS.ensure_upload_available()
    except QuotaExceeded as e:
        return jsonify({"error": str(e), "quota": e.quota_snapshot}), 429
    cap_type     = data.get("capture_type", "explicit")  # explicit | latest_freeze | aiboard
    board_id     = data.get("board_id", 0)
    ts           = datetime.datetime.now(datetime.timezone.utc)

    def _delete_old_files(cap):
        for fkey in ("original_file", "aiboard_file"):
            if cap.get(fkey):
                _delete_stored_file_best_effort("session_images", cap[fkey], session_id=sid)

    replacement_idx = None
    replacement_cap = None

    # latest_freeze: keep only the newest auto-freeze for each board in the session
    if cap_type == "latest_freeze":
        replacement_idx = next((i for i, c in enumerate(s["captures"])
                                if c.get("capture_type") == "latest_freeze"
                                and c.get("board_id") == board_id), None)

    # aiboard: keep one per board_id — replace previous for same board
    elif cap_type == "aiboard":
        replacement_idx = next((i for i, c in enumerate(s["captures"])
                                if c.get("capture_type") == "aiboard"
                                and c.get("board_id") == board_id), None)
    if replacement_idx is not None:
        replacement_cap = s["captures"][replacement_idx]

    # explicit: always keep, never replace
    cap_id = f"cap{int(ts.timestamp())}_{cap_type[0]}"
    cap = {
        "id":            cap_id,
        "timestamp":     ts.isoformat(),
        "topic":         data.get("topic", ""),
        "capture_type":  cap_type,
        "board_id":      board_id,
        "original_file": None,
        "aiboard_file":  None,
    }

    def _save_img(key, suffix):
        raw = data.get(key, "")
        if not raw:
            return None
        stored = STORAGE.save_data_url(
            raw,
            "session_images",
            f"{sid}_{cap_id}_{suffix}.png",
            owner_id=_storage_user_id(),
        )
        return stored.filename

    try:
        cap["original_file"] = _save_img("original", "original")
        cap["aiboard_file"]  = _save_img("aiboard",  "aiboard")
    except Exception as e:
        _delete_old_files(cap)
        log_warning(SECURITY["logger"], "session_capture_image_save_failed", session_id=sid, error=str(e))
        return jsonify({"error": "Could not save capture image", "details": str(e)}), 500

    if replacement_idx is not None:
        s["captures"].pop(replacement_idx)
    s["captures"].append(cap)
    try:
        _session_save(s)
    except Exception as e:
        _delete_old_files(cap)
        log_warning(SECURITY["logger"], "session_capture_save_failed", session_id=sid, error=str(e))
        return jsonify({"error": "Could not save capture to class history", "details": str(e)}), 500
    if replacement_cap:
        _delete_old_files(replacement_cap)
    QUOTAS.record_event("upload", {"route": "/api/sessions/capture", "capture_type": cap_type})
    return jsonify(cap)


@app.route("/api/sessions/<sid>", methods=["DELETE"])
@write_api_limit
@login_required_api
def api_delete_session(sid):
    s = _session_get(sid)
    if not s:
        return jsonify({"error": "Not found"}), 404
    if _use_supabase_session_store():
        try:
            cfg = supabase_config()
            resp = requests.delete(
                f"{cfg['url']}/rest/v1/class_sessions",
                headers=_supabase_persistence_headers(),
                params=[
                    ("id", f"eq.{sid}"),
                    ("user_id", f"eq.{_current_user_id_required()}"),
                ],
                timeout=20,
            )
            resp.raise_for_status()
            for cap in s.get("captures", []):
                for fkey in ("original_file", "aiboard_file"):
                    if cap.get(fkey):
                        _delete_stored_file_best_effort("session_images", cap[fkey], session_id=sid)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    for cap in s.get("captures", []):
        for fkey in ("original_file", "aiboard_file"):
            if cap.get(fkey):
                _delete_stored_file_best_effort("session_images", cap[fkey], session_id=sid)
    try: os.remove(STORAGE.session_json_path(sid))
    except: pass
    return jsonify({"ok": True})


@app.route("/api/sessions/<sid>/captures/<cap_id>", methods=["DELETE"])
@write_api_limit
@login_required_api
def api_delete_capture(sid, cap_id):
    s = _session_get(sid)
    if not s:
        return jsonify({"error": "Not found"}), 404
    idx = next((i for i, c in enumerate(s["captures"]) if c["id"] == cap_id), None)
    if idx is None:
        return jsonify({"error": "Capture not found"}), 404
    cap = s["captures"].pop(idx)
    try:
        _session_save(s)
    except Exception as e:
        return jsonify({"error": "Could not update class session", "details": str(e)}), 500
    for fkey in ("original_file", "aiboard_file"):
        if cap.get(fkey):
            _delete_stored_file_best_effort("session_images", cap[fkey], session_id=sid, capture_id=cap_id)
    return jsonify({"ok": True})


@app.route("/api/sessions/by-subject/<subject>", methods=["GET"])
@read_api_limit
@login_required_api
def api_by_subject(subject):
    all_s = _sessions_list()
    return jsonify([s for s in all_s
                    if s.get("subject", "").lower() == subject.lower()])


@app.route("/api/demo-images", methods=["GET"])
@read_api_limit
@login_required_api
def api_demo_images():
    subject = (request.args.get("subject") or "").strip().lower()
    rows = []
    for row in _load_demo_images():
        if not row.get("active", True):
            continue
        row_subject = (row.get("subject") or "All").strip().lower()
        if subject and row_subject not in ("", "all", subject):
            continue
        rows.append(_demo_image_payload(row))
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return jsonify(rows)


@app.route("/api/images/<path:filename>")
@login_required_api
def api_image(filename):
    return _serve_stored_file("session_images", filename, user_scoped=True)


@app.route("/api/demo-images/<path:filename>")
@read_api_limit
@login_required_api
def api_demo_image_file(filename):
    return _serve_stored_file("demo_images", filename)


@app.route("/api/history-images/<path:filename>")
@read_api_limit
@login_required_api
def api_history_image(filename):
    return _serve_stored_file("history_uploads", filename, user_scoped=True)


@app.route("/api/dashboard", methods=["GET"])
@read_api_limit
@login_required_api
def api_dashboard():
    quota_snapshot = QUOTAS.quota_snapshot()
    recent = QUOTAS.recent_activity(limit=8)
    total_analyses = list_history_records_paginated(page=1, per_page=1)["total"]
    return jsonify({
        "total_analyses": total_analyses,
        "analyses_this_month": count_history_records_this_month(),
        "remaining_quota": quota_snapshot,
        "recent_activity": recent,
    })


def _admin_rest_get(path: str, params=None):
    cfg = supabase_config()
    resp = requests.get(
        f"{cfg['url']}{path}",
        headers=supabase_service_headers(),
        params=params or [],
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json(), resp.headers


def _admin_rest_upsert(path: str, payload: dict):
    cfg = supabase_config()
    resp = requests.post(
        f"{cfg['url']}{path}",
        headers={**supabase_service_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return data[0] if isinstance(data, list) and data else data


def _admin_auth_users() -> list[dict]:
    cfg = supabase_config()
    resp = requests.get(
        f"{cfg['url']}/auth/v1/admin/users",
        headers=supabase_service_headers(),
        params=[("page", "1"), ("per_page", "500")],
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    return payload.get("users", [])


def _admin_try_fetch(fetcher, default):
    try:
        return fetcher(), None
    except Exception as e:
        return default, str(e)


@app.route("/api/admin/overview", methods=["GET"])
@read_api_limit
@admin_required_api
def api_admin_overview():
    warnings = []
    storage_summary = STORAGE.storage_summary()
    if storage_summary["durable_required"] and storage_summary["backend"] != "supabase_storage":
        warnings.append("storage: durable Supabase Storage is required but not configured")
    if not _allow_local_session_fallback() and not _use_supabase_session_store():
        warnings.append("sessions: Supabase session storage is required but not currently available")
    profile_rows, err = _admin_try_fetch(
        lambda: _admin_rest_get("/rest/v1/users", [
            ("select", "id,email,full_name,created_at"),
            ("order", "created_at.desc"),
        ])[0],
        [],
    )
    if err:
        warnings.append(f"profiles: {err}")

    auth_users, err = _admin_try_fetch(_admin_auth_users, [])
    if err:
        warnings.append(f"auth_users: {err}")

    overrides, err = _admin_try_fetch(
        lambda: _admin_rest_get("/rest/v1/user_quota_overrides", [
            ("select", "user_id,daily_analyses_limit,monthly_analyses_limit,daily_upload_limit,notes,updated_at"),
        ])[0],
        [],
    )
    if err:
        warnings.append(f"quota_overrides: {err}")

    history, err = _admin_try_fetch(
        lambda: _admin_rest_get("/rest/v1/analysis_history", [
            ("select", "user_id,created_at"),
            ("order", "created_at.desc"),
            ("limit", "5000"),
        ])[0],
        [],
    )
    if err:
        warnings.append(f"history: {err}")

    usage, err = _admin_try_fetch(
        lambda: _admin_rest_get("/rest/v1/usage_events", [
            ("select", "user_id,event_type,created_at"),
            ("order", "created_at.desc"),
            ("limit", "5000"),
        ])[0],
        [],
    )
    if err:
        warnings.append(f"usage: {err}")

    profile_map = {item["id"]: item for item in profile_rows}
    override_map = {item["user_id"]: item for item in overrides}
    usage_map = {}
    for item in usage:
        entry = usage_map.setdefault(item["user_id"], {"analysis": 0, "upload": 0, "last_activity": item.get("created_at")})
        if item.get("event_type") == "analysis":
            entry["analysis"] += 1
        if item.get("event_type") == "upload":
            entry["upload"] += 1
        if item.get("created_at") and (not entry["last_activity"] or item["created_at"] > entry["last_activity"]):
            entry["last_activity"] = item["created_at"]

    history_map = {}
    for item in history:
        entry = history_map.setdefault(item["user_id"], {"history_count": 0, "last_analysis_at": item.get("created_at")})
        entry["history_count"] += 1
        if item.get("created_at") and (not entry["last_analysis_at"] or item["created_at"] > entry["last_analysis_at"]):
            entry["last_analysis_at"] = item["created_at"]

    users_payload = []
    source_users = auth_users or [{"id": row["id"], "email": row.get("email"), "created_at": row.get("created_at"), "user_metadata": {"full_name": row.get("full_name")}} for row in profile_rows]
    for auth_user in source_users:
        profile = profile_map.get(auth_user["id"], {})
        user = {
            "id": auth_user["id"],
            "email": auth_user.get("email") or profile.get("email"),
            "full_name": (auth_user.get("user_metadata") or {}).get("full_name") or profile.get("full_name"),
            "created_at": auth_user.get("created_at") or profile.get("created_at"),
        }
        override = override_map.get(user["id"], {})
        activity = usage_map.get(user["id"], {})
        hist = history_map.get(user["id"], {})
        users_payload.append({
            **user,
            "is_admin": is_admin_user(user),
            "usage": activity,
            "history_count": hist.get("history_count", 0),
            "last_analysis_at": hist.get("last_analysis_at"),
            "quota_override": override,
            "effective_limits": {
                "daily_analyses": override.get("daily_analyses_limit") or QUOTAS.daily_analyses_limit,
                "monthly_analyses": override.get("monthly_analyses_limit") or QUOTAS.monthly_analyses_limit,
                "daily_uploads": override.get("daily_upload_limit") or QUOTAS.daily_upload_limit,
            },
        })

    return jsonify({
        "admin_email": (current_user() or {}).get("email", ""),
        "users": users_payload,
        "warnings": warnings,
        "defaults": {
            "daily_analyses": QUOTAS.daily_analyses_limit,
            "monthly_analyses": QUOTAS.monthly_analyses_limit,
            "daily_uploads": QUOTAS.daily_upload_limit,
        },
        "storage": storage_summary,
    })


@app.route("/api/admin/users/<user_id>/quota", methods=["POST"])
@write_api_limit
@admin_required_api
def api_admin_user_quota(user_id):
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    record = _admin_rest_upsert("/rest/v1/user_quota_overrides", {
        "user_id": user_id,
        "daily_analyses_limit": int(data.get("daily_analyses_limit") or 0) or None,
        "monthly_analyses_limit": int(data.get("monthly_analyses_limit") or 0) or None,
        "daily_upload_limit": int(data.get("daily_upload_limit") or 0) or None,
        "notes": (data.get("notes") or "").strip(),
    })
    return jsonify({"ok": True, "record": record})


@app.route("/api/admin/demo-images", methods=["GET"])
@read_api_limit
@admin_required_api
def api_admin_demo_images():
    rows = [_demo_image_payload(row) for row in _load_demo_images()]
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return jsonify(rows)


@app.route("/api/admin/demo-images", methods=["POST"])
@write_api_limit
@admin_required_api
def api_admin_create_demo_image():
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    image_data = (data.get("image_data") or "").strip()
    if not image_data:
        return jsonify({"error": "Demo image is required"}), 400
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    image_id = uuid.uuid4().hex
    try:
        stored = STORAGE.save_data_url(image_data, "demo_images", f"demo_{image_id}.png")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    row = {
        "id": image_id,
        "title": (data.get("title") or "Demo image").strip()[:120] or "Demo image",
        "caption": (data.get("caption") or "").strip()[:300],
        "subject": (data.get("subject") or "All").strip()[:60] or "All",
        "filename": stored.filename,
        "active": bool(data.get("active", True)),
        "created_at": now,
        "updated_at": now,
        "created_by": (current_user() or {}).get("email", ""),
    }
    rows = _load_demo_images()
    rows.append(row)
    _save_demo_images(rows)
    return jsonify(_demo_image_payload(row))


@app.route("/api/admin/demo-images/<image_id>", methods=["PATCH"])
@write_api_limit
@admin_required_api
def api_admin_update_demo_image(image_id):
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    rows = _load_demo_images()
    row = next((item for item in rows if item.get("id") == image_id), None)
    if not row:
        return jsonify({"error": "Demo image not found"}), 404
    if "title" in data:
        row["title"] = (data.get("title") or "Demo image").strip()[:120] or "Demo image"
    if "caption" in data:
        row["caption"] = (data.get("caption") or "").strip()[:300]
    if "subject" in data:
        row["subject"] = (data.get("subject") or "All").strip()[:60] or "All"
    if "active" in data:
        row["active"] = bool(data.get("active"))
    row["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _save_demo_images(rows)
    return jsonify(_demo_image_payload(row))


@app.route("/api/admin/demo-images/<image_id>", methods=["DELETE"])
@write_api_limit
@admin_required_api
def api_admin_delete_demo_image(image_id):
    rows = _load_demo_images()
    idx = next((i for i, item in enumerate(rows) if item.get("id") == image_id), None)
    if idx is None:
        return jsonify({"error": "Demo image not found"}), 404
    row = rows.pop(idx)
    _delete_stored_file_best_effort("demo_images", row.get("filename"), demo_image_id=image_id)
    _save_demo_images(rows)
    return jsonify({"ok": True})



@app.route("/api/history", methods=["GET"])
@read_api_limit
@login_required_api
def api_history():
    try:
        page = max(1, int(request.args.get("page", "1")))
        per_page = max(1, min(25, int(request.args.get("per_page", "12"))))
        search = request.args.get("search", "")
        subject = request.args.get("subject", "")
        return jsonify(list_history_records_paginated(page=page, per_page=per_page, search=search, subject=subject))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history", methods=["POST"])
@write_api_limit
@login_required_api
def api_create_history():
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    try:
        image_path = None
        if data.get("image_data", "").strip():
            QUOTAS.ensure_upload_available()
            stored = STORAGE.save_data_url(
                data.get("image_data", "").strip(),
                "history_uploads",
                f"history_{uuid.uuid4().hex}.png",
                owner_id=_storage_user_id(),
            )
            image_path = stored.filename
            QUOTAS.record_event("upload", {"route": "/api/history", "filename": stored.filename})
        record = insert_history_record({
            "subject": data.get("subject", "").strip() or None,
            "teacher": data.get("teacher", "").strip() or None,
            "session_id": data.get("session_id", "").strip() or None,
            "board_id": data.get("board_id"),
            "topic": data.get("topic", "").strip() or None,
            "analysis_text": data.get("analysis_text", "").strip(),
            "ocr_text": data.get("ocr_text", "").strip() or None,
            "ai_response": data.get("ai_response", "").strip() or data.get("analysis_text", "").strip(),
            "board_svg": data.get("board_svg", "").strip() or None,
            "image_path": image_path,
        })
        return jsonify(record or {"ok": True})
    except QuotaExceeded as e:
        return jsonify({"error": str(e), "quota": e.quota_snapshot}), 429
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Claude functions ──────────────────────────────────────────────────────────

def _img_msg(image_bytes: bytes, text: str) -> list:
    """Build a Claude user message with an image + text."""
    return [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(image_bytes).decode(),
                },
            },
            {"type": "text", "text": text},
        ],
    }]


def _gen_svg_incremental(image_bytes: bytes, previous_svg: str,
                         custom_prompt: str = None) -> dict:
    """Update an existing board SVG with whatever the teacher has added.
    Returns {svg, is_new_board}. Red fill marks uncertain readings."""
    try:
        # Truncate previous SVG if very long (keep structure, drop verbose style attrs)
        prev = previous_svg[:6000] if len(previous_svg) > 6000 else previous_svg

        prompt = (custom_prompt if custom_prompt else "") + f"""
You are updating a classroom whiteboard AI Board SVG.

TASK
Compare the camera image (current whiteboard) to the PREVIOUS SVG below.

Step 1 — DETECT if this is a new board:
  • If the whiteboard has been ERASED and shows substantially different/new content,
    insert this exact comment as the very first line inside the <svg> tag:
    <!-- NEW_BOARD_DETECTED -->

Step 2 — UPDATE the SVG:
  • If it's the SAME board with more content, return a complete updated SVG that
    includes EVERYTHING from the previous board PLUS the new additions.
  • Keep the same visual style (cream background #f8f8f4, Georgia serif font,
    dashed section dividers, dark text #1a1a1a).

Step 3 — MARK uncertain text in RED:
  • Any text you had to GUESS (unclear handwriting, partially visible) must use
    fill="#cc2200" on that SVG text element, so the student can verify against
    the original frozen image.

PREVIOUS BOARD SVG:
{prev}

Return ONLY raw SVG code starting with <svg. No markdown, no explanation."""

        image_b64 = base64.standard_b64encode(image_bytes).decode()
        resp = _claude.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png",
                                "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        svg = resp.content[0].text.strip()
        if "```" in svg:
            for part in svg.split("```"):
                p = part.strip().lstrip("svg").strip()
                if p.startswith("<svg"):
                    svg = p
                    break
        svg = re.sub(r"<script[^>]*>.*?</script>", "", svg,
                     flags=re.DOTALL | re.IGNORECASE)
        is_new_board = "<!-- NEW_BOARD_DETECTED -->" in svg
        svg = svg.replace("<!-- NEW_BOARD_DETECTED -->", "").strip()
        if "<svg" not in svg:
            return {
                "svg": "",
                "is_new_board": False,
                "error": "Claude returned an invalid AI Board SVG.",
            }
        return {"svg": svg, "is_new_board": is_new_board, "error": ""}
    except Exception as e:
        print(f"Incremental SVG error: {e}")
        return {"svg": "", "is_new_board": False, "error": f"AI Board update failed: {e}"}


def _gen_svg(image_bytes: bytes, custom_prompt: str = None) -> dict:
    """Generate a clean SVG recreation of the whiteboard."""
    try:
        prompt = custom_prompt if custom_prompt else """Recreate this classroom whiteboard as a clean, readable SVG image.

STYLE — match these exactly:
• SVG: viewBox="0 0 900 620" xmlns="http://www.w3.org/2000/svg"
• Outer rect: x=10 y=10 w=880 h=600 rx=8 fill="#f8f8f4" stroke="#c0b8a0" stroke-width="3"
• Inner rect: x=20 y=20 w=860 h=580 rx=4 fill="#fdfdf8" stroke="#d0c8b0" stroke-width="1.5"
• All text: font-family="Georgia, serif" fill="#1a1a1a"
• Section dividers: dashed lines stroke="#c8c0a8" stroke-dasharray="6,4" stroke-width="1.5"
• Question labels bold: font-weight="bold"
• Colours: bar chart fill="#5a8bbf", line graph stroke="#c0392b",
  pie slices fill="#4a7fc1" and fill="#e8c97a"

CONTENT — read everything on the whiteboard and:
• Replace ALL handwriting with clean typed Georgia serif text
• Use proper maths symbols: ∴ ≠ × ÷ + − = √  (use Unicode directly in SVG text nodes)
• Recreate any diagrams cleanly: circles/paths for smiley faces, <path> arcs for pie
  charts, <rect> for bar charts, <polyline>/<path> for line graphs
• Match the spatial layout exactly — preserve which quadrant each item is in

Return ONLY the raw SVG code, starting with <svg and ending with </svg>.
No explanation. No markdown. No code fences."""

        resp = _claude.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            messages=_img_msg(image_bytes, prompt),
        )
        svg = resp.content[0].text.strip()

        # Strip any markdown fences Claude might add
        if "```" in svg:
            for part in svg.split("```"):
                p = part.strip().lstrip("svg").strip()
                if p.startswith("<svg"):
                    svg = p
                    break

        # Safety: strip any <script> tags
        svg = re.sub(r"<script[^>]*>.*?</script>", "", svg,
                     flags=re.DOTALL | re.IGNORECASE)

        if "<svg" not in svg:
            return {"svg": "", "error": "Claude returned an invalid AI Board SVG."}
        return {"svg": svg, "error": ""}
    except Exception as e:
        print(f"SVG generation error: {e}")
        return {"svg": "", "error": f"AI Board generation failed: {e}"}


def _gen_analysis(image_bytes: bytes, custom_prompt: str = None) -> dict:
    """Generate a student-friendly explanation of the class content."""

    DEFAULT_PROMPT = """You are an enthusiastic, patient teacher. A student with low vision has shared a photo of their classroom whiteboard. Help them understand the lesson fully.

Read the whiteboard carefully. Then provide a clear, confident explanation structured like this:

📚 LESSON TOPIC
[Identify the subject and what is being taught today — be specific]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Walk through EVERYTHING on the board — one section per topic, question, or concept. Explain each item step by step, using plain language and examples a student would find helpful. If there are problems to solve, show the working. If there are diagrams, explain what they show and why they matter.]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🤔 YOU MIGHT WONDER:
• [A question a student would commonly ask about this topic]
• [Another natural follow-up question]
• [A third question about something tricky or interesting]

💬 Ask me anything about the lesson — I am here to help!

---
Rules:
- Be CONFIDENT and DIRECT. Never say it appears, I think, or it seems.
- This works for ANY subject — maths, science, history, English, etc.
- Write warmly and encouragingly, like a teacher who wants the student to succeed."""

    try:
        prompt = custom_prompt if custom_prompt else DEFAULT_PROMPT

        resp = _claude.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            messages=_img_msg(image_bytes, prompt),
        )
        text = resp.content[0].text.strip()
        if not text:
            return {"analysis": "", "error": "Claude returned an empty analysis."}
        return {"analysis": text, "error": ""}
    except Exception as e:
        print(f"Analysis error: {e}")
        return {"analysis": "", "error": f"Could not generate analysis: {e}"}


# ── Start ─────────────────────────────────────────────────────────────────────

def _open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    print(f"\n{'='*50}")
    print("  Lumen")
    print(f"  Claude AI : {'✓ enabled' if CLAUDE_AVAILABLE else '✗ not configured'}")
    print(f"  Browser   : http://localhost:{PORT}")
    print("  Quit      : Ctrl+C")
    print(f"{'='*50}\n")
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
