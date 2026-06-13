# VisualAssistCam Architecture

## Overview

VisualAssistCam is a whiteboard accessibility tool with two distinct application paths in the same repository:

1. A Flask-based web application for classroom capture, AI whiteboard reconstruction, lesson analysis, chat follow-ups, and session galleries.
2. A macOS-only PyQt desktop application focused on live camera zoom, image enhancement, freeze-frame OCR, and a local "Smart Board" overlay.

The web app is the primary multi-device experience. It uses the browser for camera access, OpenCV on the server for whiteboard edge detection, and Anthropic Claude for SVG reconstruction and lesson explanation.

## Repository Structure

| Path | Purpose |
|---|---|
| `app.py` | Main Flask server for local/macOS use. Binds to `127.0.0.1:5050` and auto-opens a browser. |
| `app_pi.py` | Raspberry Pi variant of the Flask server. Binds to `0.0.0.0:5050`, supports HTTPS, and stores data under `~/VisualAssistCam`. |
| `templates/index.html` | Single-page frontend for the web app. Contains HTML, CSS, and all browser-side JavaScript. |
| `main.py` | Separate PyQt6 desktop app for macOS live viewing, OCR, and Smart Board generation. |
| `requirements.txt` | Python dependencies for the PyQt desktop app. |
| `requirements_pi.txt` | Python dependencies for the Raspberry Pi Flask deployment. |
| `setup.sh` | macOS setup script for the PyQt app environment. Installs Homebrew/Tesseract and Python packages. |
| `run.sh` | Launches `app.py` from the virtual environment. |
| `setup_pi.sh` | Raspberry Pi provisioning script. Installs system packages, Python packages, SSL certs, and `.env`. |
| `run_pi.sh` | Launches `app_pi.py` from the virtual environment. |
| `setup_autostart.sh` | Creates a `systemd` service for auto-start on Raspberry Pi boot. |
| `README_PI.md` | User-facing Raspberry Pi deployment guide. |

## Runtime Variants

### 1. Flask web app

The web app is a server-rendered SPA:

- Flask serves `templates/index.html`.
- The frontend uses `navigator.mediaDevices.getUserMedia()` to access the camera.
- The frontend draws video frames into a `<canvas>` and sends frozen frames to the backend as base64 PNGs.
- The backend performs whiteboard detection, session persistence, and Claude API calls.

There are two server variants:

- `app.py`: local desktop mode for a single machine.
- `app_pi.py`: Raspberry Pi classroom mode for access from other devices on the same network.

### 2. PyQt desktop app

`main.py` is a separate product, not a thin wrapper around the Flask app.

It provides:

- native macOS camera capture via OpenCV AVFoundation
- digital zoom/pan and image enhancement
- freeze-frame OCR using Apple Vision when available
- Tesseract fallback OCR
- a generated "Smart Board" overlay rendered locally with Pillow

This path does not use Flask, the browser UI, or the Claude API.

## Web Application Components

### Backend responsibilities

The Flask backend handles:

- serving the frontend
- session CRUD APIs
- image persistence for captures and AI board images
- whiteboard calibration using OpenCV
- Claude calls for:
  - SVG whiteboard reconstruction
  - incremental SVG board updates
  - lesson analysis text
  - follow-up chat responses

### Frontend responsibilities

The browser frontend handles:

- camera enumeration and selection
- live preview rendering in a canvas
- zoom, pan, filters, invert, and fullscreen
- auto-freeze timers
- calibration requests
- AI Board view management
- AI Analysis chat UX
- session picker and gallery views
- conversion of SVG to PNG for gallery storage

## HTTP/API Surface

### UI route

- `GET /`: render the single-page application

### AI routes

- `POST /analyze`: generate AI analysis text and, in parallel, an SVG board
- `POST /update-board`: generate or incrementally update the AI board SVG
- `POST /chat`: continue lesson discussion using prior chat history and analysis context
- `POST /calibrate`: detect whiteboard corners and return perspective transform data

### File route

- `POST /save`: save a captured PNG to the user's capture directory

### Session routes

- `GET /api/sessions`
- `POST /api/sessions`
- `GET /api/sessions/<sid>`
- `DELETE /api/sessions/<sid>`
- `POST /api/sessions/<sid>/lock`
- `POST /api/sessions/<sid>/capture`
- `DELETE /api/sessions/<sid>/captures/<cap_id>`
- `GET /api/sessions/by-subject/<subject>`
- `GET /api/images/<filename>`

## Data Model

Session data is stored as JSON files on disk, one file per session.

Session shape:

```json
{
  "id": "12-char-id",
  "subject": "Maths",
  "teacher": "Name",
  "created_at": "ISO timestamp",
  "locked": false,
  "captures": [
    {
      "id": "cap...",
      "timestamp": "ISO timestamp",
      "topic": "Lesson topic",
      "capture_type": "explicit | latest_freeze | aiboard",
      "board_id": 0,
      "original_file": "optional PNG filename",
      "aiboard_file": "optional PNG filename"
    }
  ]
}
```

Capture semantics:

- `explicit`: user-triggered permanent capture
- `latest_freeze`: rolling latest frozen image for a board
- `aiboard`: rolling AI-generated board image for a board

The app replaces older `latest_freeze` and `aiboard` captures for the same `board_id`, but keeps all `explicit` captures.

## Storage Layout

### macOS Flask server

- captures: `~/Desktop/VisualAssistCam_Captures`
- sessions: `~/Desktop/VisualAssistCam_Sessions`
- gallery images: `~/Desktop/VisualAssistCam_Sessions/images`

### Raspberry Pi Flask server

- base directory: `~/VisualAssistCam`
- captures: `~/VisualAssistCam/captures`
- sessions: `~/VisualAssistCam/sessions`
- gallery images: `~/VisualAssistCam/sessions/images`

### PyQt desktop app

- captures: `~/Desktop/VisualAssistCam_Captures`

## Dependency Breakdown

### Shared web-app Python dependencies

From code and setup scripts, the Flask path depends on:

- `flask`
- `anthropic`
- `numpy`
- `python-dotenv`
- OpenCV
- standard library modules for JSON, threading, base64, sockets, file I/O

On Raspberry Pi, OpenCV is explicitly the headless build:

- `opencv-python-headless`

### PyQt desktop dependencies

`main.py` depends on:

- `PyQt6`
- `opencv-python`
- `numpy`
- `pillow`
- `pytesseract`
- `pyobjc-framework-Vision`
- `pyobjc-framework-Quartz`

It also expects the Tesseract binary at:

- `/opt/homebrew/bin/tesseract`

### External service dependency

The AI features require:

- `ANTHROPIC_API_KEY` in `.env`

Without that key:

- AI Board generation is disabled
- AI Analysis is disabled
- chat replies are disabled

### Browser dependency

The frontend loads one external JS library from CDN:

- `marked` from `cdn.jsdelivr.net`

This is used only for rendering teacher responses in the analysis chat view.

## Deployment Requirements

### Local/macOS web mode

Requirements:

- Python 3 with `venv`
- browser with camera support
- network access for Claude API
- optional `.env` with `ANTHROPIC_API_KEY`

Operational model:

- run `run.sh`
- Flask listens on `http://127.0.0.1:5050`
- a browser tab opens automatically

### Raspberry Pi mode

Requirements:

- Raspberry Pi 5 recommended
- Raspberry Pi OS 64-bit
- Python virtual environment support
- system packages installed by `setup_pi.sh`
- webcam or compatible camera
- network access for Anthropic API
- HTTPS support for browser camera access on non-localhost devices

Operational model:

- run `setup_pi.sh` once
- optional `setup_autostart.sh` for `systemd`
- run `run_pi.sh`
- Flask listens on `0.0.0.0:5050`
- prefers HTTPS using `cert.pem` and `key.pem`
- falls back to adhoc OpenSSL cert if available
- falls back to HTTP only if SSL support is unavailable

Important browser constraint:

- camera access from other devices generally requires HTTPS, which is why the Pi path provisions a self-signed certificate

### macOS PyQt mode

Requirements:

- macOS
- camera available through AVFoundation
- PyQt6
- OpenCV
- Apple Vision framework for best OCR, or Tesseract as fallback

Operational model:

- `setup.sh` provisions Homebrew and Tesseract
- the app runs locally as a desktop window

## Concurrency and Execution Model

The Flask app uses:

- one request thread per incoming Flask request
- a `ThreadPoolExecutor(max_workers=2)` for parallel AI tasks

`/analyze` submits:

- `_gen_svg(...)`
- `_gen_analysis(...)`

and waits for both before returning one response.

This keeps SVG generation and lesson analysis parallelized while preserving a simple synchronous API for the frontend.

## Data Flow

### Core whiteboard flow

1. The browser opens the app and requests camera access.
2. Live camera frames are drawn into `mainCanvas`.
3. The user freezes the current frame.
4. The frontend captures the frozen canvas as a base64 PNG.
5. The frontend immediately:
   - stores the frozen frame in the current session as `latest_freeze`
   - calls `POST /update-board` to generate or update the AI board
6. The backend decodes the image and, if a prior SVG exists, uses incremental board generation.
7. Claude returns raw SVG.
8. The frontend renders the SVG into the AI Board view.
9. The frontend rasterizes that SVG to PNG and stores it as an `aiboard` capture.

### Analysis/chat flow

1. After a frame is frozen, the user chooses "Analyse Board".
2. The frontend calls `POST /analyze` with:
   - the frozen PNG
   - custom analysis prompt
   - blank SVG prompt when only analysis is needed in that view
3. The backend still executes the same analysis pathway and returns analysis text.
4. The frontend renders the teacher response, extracts suggested questions, and enables chat input.
5. Follow-up questions call `POST /chat` with:
   - current message
   - prior chat history
   - original analysis context
6. Claude returns plain-text follow-up teaching responses.

### Calibration flow

1. The frontend captures the current canvas and posts it to `/calibrate`.
2. The backend decodes the image and runs `_find_whiteboard(...)`.
3. If a quadrilateral is found, the backend returns:
   - ordered corners
   - homography matrix
   - output size
   - frame size
4. The frontend stores calibration metadata and performs perspective correction in the browser render loop using triangle-based warping.

### Gallery/session flow

1. Session creation writes a JSON file immediately.
2. Captures write PNG files into the session image directory.
3. Session metadata stores only filenames, not inline image data.
4. Gallery screens fetch sessions by subject and render image URLs from `/api/images/<filename>`.
5. Deleting captures or sessions removes both metadata and associated files where present.

## AI Integration Details

The web app uses Anthropic Claude multimodal requests with the frozen frame embedded as base64 image content.

Main AI tasks:

- `_gen_svg(...)`: create a clean SVG reconstruction of the whiteboard
- `_gen_svg_incremental(...)`: update a prior SVG and detect whether the board was erased
- `_gen_analysis(...)`: explain the lesson in student-friendly text
- `/chat`: continue tutoring from the original analysis context

Response hardening already present:

- strips Markdown fences from SVG responses
- strips `<script>` tags from SVG before rendering
- detects `<!-- NEW_BOARD_DETECTED -->` marker in incremental updates

## Frontend State Model

The SPA relies on in-page global state rather than a framework.

Important state variables include:

- `currentSession`
- `lastOrigPng`
- `lastSvgString`
- `previousSvg`
- `boardId`
- `boardFrozen`
- calibration state such as `calCorners` and `calActive`
- camera display state such as zoom/pan/filter values
- chat state such as `chatHistory` and `currentAnalysisContext`

This keeps the app simple to ship, but couples many features into one large template file.

## Architectural Observations

### Strengths

- Very small deployment footprint.
- No database requirement; file-based persistence is easy to back up.
- Clear separation between local desktop mode and Pi classroom mode.
- Browser camera capture avoids device-specific native camera code in the web path.
- Incremental AI board update is a good fit for evolving whiteboard lessons.

### Tradeoffs

- Most frontend logic lives in one large `templates/index.html` file.
- Session storage is file-based and not designed for concurrent multi-user writes.
- The web and PyQt paths overlap conceptually but do not share much code.
- Prompt text and UI logic are tightly coupled to the backend response shape.
- The Pi requirements are documented and scripted, but the Mac Flask path has no dedicated requirements file separate from the PyQt app.

## Suggested Mental Model

Think of the repository as three layers:

1. Capture/UI layer
   - browser SPA in `templates/index.html`
   - or native macOS UI in `main.py`
2. Processing layer
   - OpenCV whiteboard detection
   - frontend canvas transforms
   - OCR and Smart Board generation in the PyQt path
3. AI/session layer
   - Claude multimodal calls
   - JSON session persistence
   - PNG gallery assets

For the current web product, `app.py` or `app_pi.py` plus `templates/index.html` are the architectural center of gravity.
