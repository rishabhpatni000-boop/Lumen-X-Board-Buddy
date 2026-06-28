#!/usr/bin/env python3
"""
Lumen — Raspberry Pi 5 version
All features identical to the Mac version.

Key differences from app.py (Mac):
  • Listens on 0.0.0.0 so any device on the network can connect
  • Uses HTTPS (self-signed cert via pyOpenSSL) — required for camera
    access from non-localhost devices in Chrome / Firefox
  • Data stored in ~/Lumen/ instead of ~/Desktop/
  • No browser auto-launch (Pi may be headless)
  • Shows the Pi's network IP on startup so students know the URL
"""

import os
import re
import json
import uuid
import time
import socket
import datetime
import hashlib
import numpy as np
import cv2
import base64
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for
from werkzeug.utils import secure_filename

from supabase_integration import (
    count_history_records_this_month,
    clear_auth_session,
    configure_supabase,
    current_user,
    insert_history_record,
    list_history_records_paginated,
    login_required_api,
    login_required_page,
    safe_next_url,
    store_session_from_token,
    template_auth_context,
)
from services.cache_service import TTLCache, stable_cache_key
from services.logging_service import log_event
from services.quota_service import QuotaExceeded, QuotaService
from services.storage_service import StorageService
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

# ── Data directories (Pi-friendly paths, no Desktop) ─────────────────────────
DATA_DIR = os.getenv("VISUALASSISTCAM_DATA_DIR", os.path.expanduser("~/Lumen"))

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

# ── Session helpers ───────────────────────────────────────────────────────────

def _sessions_list() -> list:
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
    p = STORAGE.session_json_path(sid)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None

def _session_save(s: dict):
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
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    message = data.get("message", "").strip()
    history = data.get("history", [])
    context = data.get("context", "")
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
        stored = STORAGE.save_data_url(img_data, "captures", data.get("filename", "capture.png"))
        QUOTAS.record_event("upload", {"route": "/save", "filename": stored.filename})
        log_event(SECURITY["logger"], "upload_saved", ip=request.remote_addr, filename=stored.filename)
        return jsonify({"ok": True, "path": stored.absolute_path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── Whiteboard calibration ────────────────────────────────────────────────────

@app.route("/calibrate", methods=["POST"])
@anon_api_quota
@login_required_api
def calibrate():
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
                "message": "Could not detect whiteboard edges. Ensure the board is well-lit.",
                "frame_size": [w, h],
                "corners": [[0,0],[w,0],[w,h],[0,h]],
                "homography": np.eye(3).tolist(),
                "output_size": [w, h],
            })
        ordered = _order_corners(corners)
        tl, tr, br, bl = ordered
        out_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
        out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
        src_pts = ordered.astype(np.float32)
        dst_pts = np.array([[0,0],[out_w,0],[out_w,out_h],[0,out_h]], dtype=np.float32)
        H, _ = cv2.findHomography(src_pts, dst_pts)
        return jsonify({
            "success": True,
            "corners": ordered.tolist(),
            "homography": H.tolist(),
            "output_size": [out_w, out_h],
            "frame_size": [w, h],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def _find_whiteboard(frame):
    h, w = frame.shape[:2]
    min_area = 0.05 * h * w
    max_area = 0.98 * h * w
    gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    best = None; best_area = 0
    for lo, hi in [(30,100),(20,80),(50,150),(10,60)]:
        edges   = cv2.Canny(blurred, lo, hi)
        dilated = cv2.dilate(edges, np.ones((3,3), np.uint8), iterations=3)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:8]:
            area = cv2.contourArea(cnt)
            if area < min_area or area > max_area: continue
            approx = cv2.approxPolyDP(cnt, 0.02 * cv2.arcLength(cnt, True), True)
            if len(approx) == 4 and area > best_area:
                best = approx.reshape(4,2).astype(np.float32); best_area = area
        if best is not None: break
    return best


def _order_corners(pts):
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1); diff = np.diff(pts, axis=1).flatten()
    rect[0]=pts[np.argmin(s)]; rect[2]=pts[np.argmax(s)]
    rect[1]=pts[np.argmin(diff)]; rect[3]=pts[np.argmax(diff)]
    return rect


# ── Board update ──────────────────────────────────────────────────────────────

@app.route("/update-board", methods=["POST"])
@anon_api_quota
@login_required_api
def update_board():
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


# ── Session API ───────────────────────────────────────────────────────────────

@app.route("/api/sessions", methods=["GET"])
@read_api_limit
@login_required_api
def api_sessions():
    return jsonify(_sessions_list())

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
    _session_save(s)
    return jsonify(s)

@app.route("/api/sessions/<sid>", methods=["GET"])
@read_api_limit
@login_required_api
def api_get_session(sid):
    s = _session_get(sid)
    return jsonify(s) if s else (jsonify({"error": "Not found"}), 404)

@app.route("/api/sessions/<sid>/lock", methods=["POST"])
@write_api_limit
@login_required_api
def api_toggle_lock(sid):
    s = _session_get(sid)
    if not s: return jsonify({"error": "Not found"}), 404
    s["locked"] = not s.get("locked", False)
    _session_save(s)
    return jsonify({"locked": s["locked"]})

@app.route("/api/sessions/<sid>/capture", methods=["POST"])
@anon_api_quota
@login_required_api
def api_add_capture(sid):
    s = _session_get(sid)
    if not s: return jsonify({"error": "Not found"}), 404
    if s.get("locked"): return jsonify({"error": "Session is locked"}), 403
    data, error_response = require_json_payload()
    if error_response:
        return error_response
    try:
        QUOTAS.ensure_upload_available()
    except QuotaExceeded as e:
        return jsonify({"error": str(e), "quota": e.quota_snapshot}), 429
    cap_type = data.get("capture_type", "explicit")
    board_id = data.get("board_id", 0)
    ts       = datetime.datetime.now(datetime.timezone.utc)

    def _del(cap):
        for fk in ("original_file","aiboard_file"):
            if cap.get(fk):
                STORAGE.delete_file("session_images", cap[fk])

    if cap_type == "latest_freeze":
        idx = next((i for i,c in enumerate(s["captures"])
                    if c.get("capture_type")=="latest_freeze"), None)
        if idx is not None: _del(s["captures"][idx]); s["captures"].pop(idx)
    elif cap_type == "aiboard":
        idx = next((i for i,c in enumerate(s["captures"])
                    if c.get("capture_type")=="aiboard" and c.get("board_id")==board_id), None)
        if idx is not None: _del(s["captures"][idx]); s["captures"].pop(idx)

    cap_id = f"cap{int(ts.timestamp())}_{cap_type[0]}"
    cap = {"id":cap_id,"timestamp":ts.isoformat(),"topic":data.get("topic",""),
           "capture_type":cap_type,"board_id":board_id,
           "original_file":None,"aiboard_file":None}

    def _save(key, suffix):
        raw = data.get(key,"")
        if not raw:
            return None
        stored = STORAGE.save_data_url(raw, "session_images", f"{sid}_{cap_id}_{suffix}.png")
        return stored.filename

    cap["original_file"] = _save("original","original")
    cap["aiboard_file"]  = _save("aiboard","aiboard")
    s["captures"].append(cap)
    _session_save(s)
    QUOTAS.record_event("upload", {"route": "/api/sessions/capture", "capture_type": cap_type})
    return jsonify(cap)

@app.route("/api/sessions/<sid>", methods=["DELETE"])
@write_api_limit
@login_required_api
def api_delete_session(sid):
    s = _session_get(sid)
    if not s: return jsonify({"error": "Not found"}), 404
    for cap in s.get("captures",[]):
        for fk in ("original_file","aiboard_file"):
            if cap.get(fk):
                STORAGE.delete_file("session_images", cap[fk])
    try: os.remove(STORAGE.session_json_path(sid))
    except: pass
    return jsonify({"ok": True})

@app.route("/api/sessions/<sid>/captures/<cap_id>", methods=["DELETE"])
@write_api_limit
@login_required_api
def api_delete_capture(sid, cap_id):
    s = _session_get(sid)
    if not s: return jsonify({"error": "Not found"}), 404
    idx = next((i for i,c in enumerate(s["captures"]) if c["id"]==cap_id), None)
    if idx is None: return jsonify({"error": "Not found"}), 404
    cap = s["captures"].pop(idx)
    for fk in ("original_file","aiboard_file"):
        if cap.get(fk):
            STORAGE.delete_file("session_images", cap[fk])
    _session_save(s)
    return jsonify({"ok": True})

@app.route("/api/sessions/by-subject/<subject>", methods=["GET"])
@read_api_limit
@login_required_api
def api_by_subject(subject):
    return jsonify([s for s in _sessions_list()
                    if s.get("subject","").lower() == subject.lower()])

@app.route("/api/images/<filename>")
@login_required_api
def api_image(filename):
    return send_from_directory(STORAGE.images_dir, filename)


@app.route("/api/history-images/<filename>")
@read_api_limit
@login_required_api
def api_history_image(filename):
    return send_from_directory(STORAGE.history_dir, filename)


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

def _img_msg(image_bytes, text):
    return [{"role":"user","content":[
        {"type":"image","source":{"type":"base64","media_type":"image/png",
         "data":base64.standard_b64encode(image_bytes).decode()}},
        {"type":"text","text":text}
    ]}]


def _gen_svg_incremental(image_bytes, previous_svg, custom_prompt=None):
    try:
        prev = previous_svg[:6000] if len(previous_svg)>6000 else previous_svg
        prompt = (custom_prompt or "") + f"""
You are updating a classroom whiteboard AI Board SVG.
Compare the camera image to the PREVIOUS SVG below.
Step 1 — If the board was ERASED, insert <!-- NEW_BOARD_DETECTED --> inside the <svg> tag.
Step 2 — Return a complete updated SVG (previous content + new additions).
Step 3 — Mark uncertain text with fill="#cc2200".
PREVIOUS BOARD SVG:
{prev}
Return ONLY raw SVG code starting with <svg. No markdown."""
        image_b64 = base64.standard_b64encode(image_bytes).decode()
        resp = _claude.messages.create(
            model="claude-opus-4-8", max_tokens=4096,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/png","data":image_b64}},
                {"type":"text","text":prompt}
            ]}])
        svg = resp.content[0].text.strip()
        if "```" in svg:
            for part in svg.split("```"):
                p = part.strip().lstrip("svg").strip()
                if p.startswith("<svg"): svg = p; break
        svg = re.sub(r"<script[^>]*>.*?</script>","",svg,flags=re.DOTALL|re.IGNORECASE)
        is_new = "<!-- NEW_BOARD_DETECTED -->" in svg
        svg = svg.replace("<!-- NEW_BOARD_DETECTED -->","").strip()
        if "<svg" not in svg:
            return {"svg":"", "is_new_board":False, "error":"Claude returned an invalid AI Board SVG."}
        return {"svg": svg, "is_new_board": is_new, "error": ""}
    except Exception as e:
        print(f"Incremental SVG error: {e}")
        return {"svg":"", "is_new_board":False, "error":f"AI Board update failed: {e}"}


def _gen_svg(image_bytes, custom_prompt=None):
    try:
        prompt = custom_prompt or """Recreate this classroom whiteboard as a clean SVG.
STYLE: viewBox="0 0 900 620", cream background #f8f8f4, Georgia serif font, dark text #1a1a1a,
dashed section dividers. Replace ALL handwriting with clean typed text. Use proper maths symbols.
Recreate diagrams as clean SVG shapes. Match the spatial layout exactly.
Return ONLY raw SVG starting with <svg. No markdown."""
        resp = _claude.messages.create(model="claude-opus-4-8", max_tokens=4096,
                                        messages=_img_msg(image_bytes, prompt))
        svg = resp.content[0].text.strip()
        if "```" in svg:
            for part in svg.split("```"):
                p = part.strip().lstrip("svg").strip()
                if p.startswith("<svg"): svg = p; break
        svg = re.sub(r"<script[^>]*>.*?</script>","",svg,flags=re.DOTALL|re.IGNORECASE)
        if "<svg" not in svg:
            return {"svg":"", "error":"Claude returned an invalid AI Board SVG."}
        return {"svg": svg, "error": ""}
    except Exception as e:
        print(f"SVG generation error: {e}")
        return {"svg":"", "error":f"AI Board generation failed: {e}"}


def _gen_analysis(image_bytes, custom_prompt=None):
    DEFAULT = """You are an enthusiastic, patient teacher. A student with low vision shared a classroom whiteboard photo.
Read it carefully and explain the lesson clearly:

📚 LESSON TOPIC
[Identify the subject and topic]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Walk through EVERYTHING on the board step by step. Use plain language.]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🤔 YOU MIGHT WONDER:
• [Likely student question 1]
• [Likely student question 2]
• [Likely student question 3]

💬 Ask me anything about the lesson!

Rules: Be CONFIDENT. Works for ANY subject. Write warmly and encouragingly."""
    try:
        resp = _claude.messages.create(
            model="claude-opus-4-8", max_tokens=1024,
            messages=_img_msg(image_bytes, custom_prompt or DEFAULT))
        text = resp.content[0].text.strip()
        if not text:
            return {"analysis": "", "error": "Claude returned an empty analysis."}
        return {"analysis": text, "error": ""}
    except Exception as e:
        print(f"Analysis error: {e}")
        return {"analysis": "", "error": f"Could not generate analysis: {e}"}


# ── Network helpers ───────────────────────────────────────────────────────────

def _get_ip():
    """Get the Pi's local network IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ip = _get_ip()

    # Check for SSL certificate (generated by setup_pi.sh)
    cert_file = os.path.join(os.path.dirname(__file__), "cert.pem")
    key_file  = os.path.join(os.path.dirname(__file__), "key.pem")

    if os.path.exists(cert_file) and os.path.exists(key_file):
        ssl_context = (cert_file, key_file)
        protocol    = "https"
    else:
        # Try pyOpenSSL adhoc cert (auto-generated, no files needed)
        try:
            import OpenSSL  # noqa — just checking it's installed
            ssl_context = "adhoc"
            protocol    = "https"
        except ImportError:
            ssl_context = None
            protocol    = "http"
            print("\n  ⚠  WARNING: Running without HTTPS.")
            print("     Camera access from other devices may be blocked by browsers.")
            print("     Run setup_pi.sh or: pip install pyopenssl\n")

    print(f"\n{'='*56}")
    print("  Lumen  —  Raspberry Pi 5")
    print(f"{'='*56}")
    print(f"  Claude AI   : {'✓ enabled' if CLAUDE_AVAILABLE else '✗ not configured — edit .env'}")
    print(f"  Data stored : {BASE_DIR}")
    print(f"{'='*56}")
    print(f"  📱 Open on THIS Pi  : {protocol}://localhost:{PORT}")
    print(f"  📱 Open on ANY device on the same WiFi:")
    print(f"      {protocol}://{ip}:{PORT}")
    if protocol == "https":
        print()
        print("  ⚠  First visit: browser will warn 'Not secure'.")
        print("     Click Advanced → Proceed to {ip} (unsafe).")
        print("     This is normal for a self-signed certificate.")
    print(f"{'='*56}")
    print("  Press Ctrl+C to quit")
    print(f"{'='*56}\n")

    app.run(
        host        = "0.0.0.0",
        port        = PORT,
        ssl_context = ssl_context,
        debug       = False,
        use_reloader= False,
    )
