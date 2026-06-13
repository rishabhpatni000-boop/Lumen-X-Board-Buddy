#!/usr/bin/env python3
"""
VisualAssistCam — three-view whiteboard assistant
Original camera | AI Board (SVG) | AI Analysis (text)
"""

import os
import re
import json
import uuid
import time
import datetime
import numpy as np
import cv2
import threading
import webbrowser
import base64
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify, render_template, send_from_directory, redirect, url_for
from werkzeug.utils import secure_filename

from supabase_integration import (
    clear_auth_session,
    configure_supabase,
    current_user,
    insert_history_record,
    list_history_records,
    login_required_api,
    login_required_page,
    safe_next_url,
    store_session_from_token,
    template_auth_context,
)
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

SAVE_DIR     = os.path.expanduser("~/Desktop/VisualAssistCam_Captures")
SESSIONS_DIR = os.path.expanduser("~/Desktop/VisualAssistCam_Sessions")
IMAGES_DIR   = os.path.join(SESSIONS_DIR, "images")
LOG_DIR      = os.path.expanduser("~/Desktop/VisualAssistCam_Logs")
for d in [SAVE_DIR, SESSIONS_DIR, IMAGES_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

SECURITY = configure_app_security(app, LOG_DIR)
anon_api_quota = SECURITY["anon_api_quota"]
read_api_limit = SECURITY["read_api_limit"]
write_api_limit = SECURITY["write_api_limit"]

# ── Session helpers ──────────────────────────────────────────────────────────

def _sessions_list() -> list:
    out = []
    for f in os.listdir(SESSIONS_DIR):
        if f.endswith(".json"):
            try:
                with open(os.path.join(SESSIONS_DIR, f)) as fp:
                    out.append(json.load(fp))
            except Exception:
                pass
    return out

def _session_get(sid: str):
    p = os.path.join(SESSIONS_DIR, f"{sid}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None

def _session_save(s: dict):
    with open(os.path.join(SESSIONS_DIR, f"{s['id']}.json"), "w") as f:
        json.dump(s, f, indent=2)

PORT = 5050
_pool = ThreadPoolExecutor(max_workers=2)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def landing():
    if current_user():
        return redirect(url_for("app_dashboard"))
    next_url = safe_next_url(request.args.get("next"))
    return render_template(
        "landing.html",
        claude_available=CLAUDE_AVAILABLE,
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


@app.route("/analyze", methods=["POST"])
@anon_api_quota
@login_required_api
def analyze():
    data, error_response = require_json_payload()
    if error_response:
        return error_response
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

    # Run SVG + analysis in parallel
    svg_f      = _pool.submit(_gen_svg,      image_bytes, custom_svg_prompt)
    analysis_f = _pool.submit(_gen_analysis, image_bytes, custom_analysis_prompt)
    return jsonify({"svg": svg_f.result(), "analysis": analysis_f.result()})


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
        image_bytes, mime_type = decode_image_data_url(img_data, app.config["MAX_IMAGE_BYTES"])
        ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }[mime_type]
        filename = secure_filename(data.get("filename", "capture.png")) or f"capture{ext}"
        if not filename.lower().endswith(ext):
            filename = f"{os.path.splitext(filename)[0]}{ext}"
        path = os.path.join(SAVE_DIR, filename)
        with open(path, "wb") as f:
            f.write(image_bytes)
        return jsonify({"ok": True, "path": path})
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

    if previous_svg.strip().startswith("<svg"):
        result = _gen_svg_incremental(image_bytes, previous_svg, svg_prompt)
    else:
        svg = _gen_svg(image_bytes, svg_prompt)
        result = {"svg": svg, "is_new_board": False}

    return jsonify(result)


# ── Session API ──────────────────────────────────────────────────────────────

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
        "created_at": datetime.datetime.now().isoformat(),
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
    if not s:
        return jsonify({"error": "Not found"}), 404
    s["locked"] = not s.get("locked", False)
    _session_save(s)
    return jsonify({"locked": s["locked"]})


@app.route("/api/sessions/<sid>/capture", methods=["POST"])
@anon_api_quota
@login_required_api
def api_add_capture(sid):
    s = _session_get(sid)
    if not s:
        return jsonify({"error": "Not found"}), 404
    if s.get("locked"):
        return jsonify({"error": "Session is locked"}), 403

    data, error_response = require_json_payload()
    if error_response:
        return error_response
    cap_type     = data.get("capture_type", "explicit")  # explicit | latest_freeze | aiboard
    board_id     = data.get("board_id", 0)
    ts           = datetime.datetime.now()

    def _delete_old_files(cap):
        for fkey in ("original_file", "aiboard_file"):
            if cap.get(fkey):
                try: os.remove(os.path.join(IMAGES_DIR, cap[fkey]))
                except: pass

    # latest_freeze: keep one per board_id — replace previous freeze from same board
    if cap_type == "latest_freeze":
        idx = next((i for i, c in enumerate(s["captures"])
                    if c.get("capture_type") == "latest_freeze"
                    and c.get("board_id") == board_id), None)
        if idx is not None:
            _delete_old_files(s["captures"][idx])
            s["captures"].pop(idx)

    # aiboard: keep one per board_id — replace previous for same board
    elif cap_type == "aiboard":
        idx = next((i for i, c in enumerate(s["captures"])
                    if c.get("capture_type") == "aiboard"
                    and c.get("board_id") == board_id), None)
        if idx is not None:
            _delete_old_files(s["captures"][idx])
            s["captures"].pop(idx)

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
        image_bytes, mime_type = decode_image_data_url(raw, app.config["MAX_IMAGE_BYTES"])
        ext = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }[mime_type]
        fname = f"{sid}_{cap_id}_{suffix}{ext}"
        with open(os.path.join(IMAGES_DIR, fname), "wb") as f:
            f.write(image_bytes)
        return fname

    cap["original_file"] = _save_img("original", "original")
    cap["aiboard_file"]  = _save_img("aiboard",  "aiboard")

    s["captures"].append(cap)
    _session_save(s)
    return jsonify(cap)


@app.route("/api/sessions/<sid>", methods=["DELETE"])
@write_api_limit
@login_required_api
def api_delete_session(sid):
    s = _session_get(sid)
    if not s:
        return jsonify({"error": "Not found"}), 404
    for cap in s.get("captures", []):
        for fkey in ("original_file", "aiboard_file"):
            if cap.get(fkey):
                try: os.remove(os.path.join(IMAGES_DIR, cap[fkey]))
                except: pass
    try: os.remove(os.path.join(SESSIONS_DIR, f"{sid}.json"))
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
    for fkey in ("original_file", "aiboard_file"):
        if cap.get(fkey):
            try: os.remove(os.path.join(IMAGES_DIR, cap[fkey]))
            except: pass
    _session_save(s)
    return jsonify({"ok": True})


@app.route("/api/sessions/by-subject/<subject>", methods=["GET"])
@read_api_limit
@login_required_api
def api_by_subject(subject):
    all_s = _sessions_list()
    return jsonify([s for s in all_s
                    if s.get("subject", "").lower() == subject.lower()])


@app.route("/api/images/<filename>")
@login_required_api
def api_image(filename):
    return send_from_directory(IMAGES_DIR, filename)


@app.route("/api/history", methods=["GET"])
@read_api_limit
@login_required_api
def api_history():
    try:
        return jsonify(list_history_records(limit=100))
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
        record = insert_history_record({
            "subject": data.get("subject", "").strip() or None,
            "teacher": data.get("teacher", "").strip() or None,
            "session_id": data.get("session_id", "").strip() or None,
            "board_id": data.get("board_id"),
            "topic": data.get("topic", "").strip() or None,
            "analysis_text": data.get("analysis_text", "").strip(),
            "board_svg": data.get("board_svg", "").strip() or None,
        })
        return jsonify(record or {"ok": True})
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
        return {"svg": svg if "<svg" in svg else "", "is_new_board": is_new_board}
    except Exception as e:
        print(f"Incremental SVG error: {e}")
        return {"svg": "", "is_new_board": False}


def _gen_svg(image_bytes: bytes, custom_prompt: str = None) -> str:
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

        return svg if "<svg" in svg else ""
    except Exception as e:
        print(f"SVG generation error: {e}")
        return ""


def _gen_analysis(image_bytes: bytes, custom_prompt: str = None) -> str:
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
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"Analysis error: {e}")
        return f"Could not generate analysis: {e}"


# ── Start ─────────────────────────────────────────────────────────────────────

def _open_browser():
    time.sleep(1.5)
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    print(f"\n{'='*50}")
    print("  VisualAssistCam")
    print(f"  Claude AI : {'✓ enabled' if CLAUDE_AVAILABLE else '✗ not configured'}")
    print(f"  Browser   : http://localhost:{PORT}")
    print("  Quit      : Ctrl+C")
    print(f"{'='*50}\n")
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
