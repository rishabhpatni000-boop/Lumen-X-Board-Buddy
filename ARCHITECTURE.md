# VisualAssistCam Architecture

## Overview

VisualAssistCam is now structured as an authenticated Flask web application with a preserved camera/OCR/Claude workflow and a separate legacy macOS PyQt application.

Primary production web flow:

Visitor -> Landing Page -> Google Sign-In -> Dashboard (`/app`) -> Analysis Features -> History -> Settings

## Application Structure

### Web entry points

- `app.py`: local/macOS Flask server
- `app_pi.py`: Raspberry Pi Flask server with HTTPS/network-friendly startup

### Frontend templates

- `templates/landing.html`: public marketing/login page
- `templates/auth_callback.html`: Supabase OAuth callback handoff
- `templates/index.html`: protected dashboard + analysis application
- `templates/history.html`: protected per-user analysis history
- `templates/settings.html`: protected quota/security settings summary
- `templates/errors/*.html`: 403/404/500 user-facing error pages

### Shared backend modules

- `web_security.py`: rate limiting, request size limits, CSRF, structured request logging, error handlers
- `supabase_integration.py`: Supabase Auth session handling and history access
- `services/storage_service.py`: centralized local file storage abstraction
- `services/quota_service.py`: per-user usage tracking and quota checks
- `services/cache_service.py`: in-memory TTL caching for duplicate analysis requests
- `services/logging_service.py`: structured application event logging helpers

### Other application paths

- `main.py`: standalone macOS PyQt viewer/OCR app

## Route Flow

### Public routes

- `/`: landing page
- `/auth/callback`: OAuth completion page
- `/auth/session`: exchange Supabase access token for Flask session

### Protected pages

- `/app`: main dashboard and analysis workspace
- `/camera`: protected alias to `/app`
- `/history`: authenticated user history
- `/settings`: quota/security/account summary

### Protected APIs

- `/analyze`
- `/chat`
- `/save`
- `/calibrate`
- `/update-board`
- `/api/dashboard`
- `/api/history`
- `/api/history-images/<filename>`
- `/api/sessions/*`
- `/api/images/<filename>`
- `/auth/logout`

Unauthenticated access is redirected back to the landing page.

## Authentication Flow

1. Visitor lands on `/`.
2. Frontend starts Google OAuth using Supabase Auth.
3. Supabase returns the browser to `/auth/callback`.
4. Frontend exchanges the OAuth code for a Supabase session.
5. Frontend posts the Supabase access token to `/auth/session`.
6. Flask verifies the user through Supabase `/auth/v1/user`.
7. Flask stores a secure server-side session cookie.
8. User is redirected to `/app`.

Logout:

- Frontend signs out from Supabase JS.
- Frontend posts to `/auth/logout`.
- Flask session is cleared.

## OCR / Analysis Pipeline

### Web application

The protected web app uses the browser camera and backend image processing:

1. Browser captures camera frames with `getUserMedia`.
2. Frames are drawn into a canvas.
3. Frozen frames are sent as base64 JSON payloads.
4. `/calibrate` uses OpenCV to detect the whiteboard quadrilateral.
5. `/update-board` generates or incrementally updates an SVG whiteboard via Claude.
6. `/analyze` generates:
   - SVG board representation
   - lesson explanation text
7. `/chat` continues lesson follow-up tutoring.

### Legacy PyQt path

`main.py` still provides native macOS OCR and Smart Board rendering using:

- Apple Vision OCR
- Tesseract fallback
- local image enhancement and zoom/pan

## AI Integration

Anthropic Claude remains the primary AI service for the web app:

- `_gen_svg(...)`
- `_gen_svg_incremental(...)`
- `_gen_analysis(...)`
- `/chat`

Protections now wrapped around the AI routes:

- authentication required
- per-IP rate limiting
- per-user quota checks
- request-size limits
- duplicate-request caching
- structured logging

## Storage System

All file handling now goes through `StorageService`.

Storage areas:

- `captures/`: explicit saved images
- `sessions/`: JSON session files
- `sessions/images/`: gallery/session images
- `history_uploads/`: user history image references
- `logs/`: application logs
- `tmp/`: temporary working area with cleanup support

The abstraction is local-disk based today but designed so a future move to Supabase Storage is isolated behind one service boundary.

## Database Usage

Supabase is used for authentication and persistent user-owned records.

### Tables

- `public.users`
- `public.analysis_history`
- `public.usage_events`

### Stored data

`analysis_history` includes:

- subject
- teacher
- session id
- board id
- topic
- OCR text
- AI response
- board SVG
- uploaded image reference
- timestamps

`usage_events` tracks:

- analyses
- AI requests
- OCR requests
- uploads

## Security Controls

Implemented controls:

- Google Sign-In through Supabase Auth
- protected routes and authenticated session gating
- row-level security in Supabase
- CSRF validation for unsafe requests
- request size limits
- base64 image validation
- filename sanitization
- path traversal protection through controlled storage roots
- per-IP rate limiting
- per-user daily/monthly quota checks
- structured request/error/event logging
- user-facing 403/404/500 pages
- secure session cookie configuration via env vars

## External Dependencies

### Web stack

- Flask
- Flask-Limiter
- Requests
- OpenCV
- NumPy
- python-dotenv
- Anthropic SDK
- Supabase JS (loaded in templates)

### Deployment integrations

- Supabase Auth
- Supabase Postgres / REST / RLS
- Google OAuth
- optional Redis for distributed rate limiting

## Operational Notes

- The web dashboard is now the center of gravity for production use.
- The PyQt app remains available but is not part of the authenticated public web flow.
- In-memory caching and in-memory rate limit storage are acceptable for single-instance development, but Redis-backed rate limiting is recommended for multi-instance deployment.
