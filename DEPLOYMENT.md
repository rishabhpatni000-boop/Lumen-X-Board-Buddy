# Deployment Instructions

## Current route structure

- `/` -> public landing page
- `/auth/callback` -> completes Supabase login
- `/app` -> protected main application dashboard
- `/camera` -> protected alias to `/app`
- `/history` -> protected per-user history page
- `/settings` -> protected quota and security summary

Protected API routes include:

- `/analyze`
- `/chat`
- `/save`
- `/calibrate`
- `/update-board`
- `/api/dashboard`
- `/api/sessions/*`
- `/api/history`
- `/api/history-images/*`
- `/api/images/*`

## Local web deployment

1. Create a virtual environment.
2. Install [requirements_web.txt](/Users/rishabhpatni/Downloads/Lumen/requirements_web.txt).
3. Copy `.env.example` to `.env` and set:
   - `FLASK_SECRET_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `ANTHROPIC_API_KEY`
4. Start the app with:

```bash
bash run.sh
```

## Custom domain deployment

The app does not need to stay on Render. Any host that can run Flask/Gunicorn over HTTPS will work as long as the domain, OAuth callback, and storage settings are updated together.

Minimum production requirements for a real domain such as `app.visualassistcam.com`:

1. Serve the app behind HTTPS.
2. Point the DNS record for the chosen domain to your host.
3. Set the final callback URL in Supabase:

```text
https://app.visualassistcam.com/auth/callback
```

4. Add the same URL in Supabase `Authentication -> URL Configuration`.
5. Set these production env vars on the host:
   - `SESSION_COOKIE_SECURE=true`
   - `TRUST_PROXY_COUNT=1` (or the correct proxy hop count)
   - `VISUALASSISTCAM_DATA_DIR=/path/to/persistent/storage`
6. Use persistent storage for captures, session JSON, and logs, or replace local storage with object storage such as S3 or Supabase Storage.
   In the current web deployment, Supabase Storage is the durable source for gallery/demo/history images.
7. Start the app with a production server such as:

```bash
gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
```

If you want both apex and `www` or a separate app subdomain, add each exact HTTPS callback URL to Supabase.

## Render deployment

### 1. Prepare the repository

1. Push this repository to GitHub.
2. Make sure `requirements_web.txt` is committed.
3. Make sure your local `.env` is **not** committed publicly unless you intentionally want those values in git.

### 2. Create the Render web service

1. In Render, click `New +` -> `Web Service`.
2. Connect the GitHub repository.
3. Choose the branch you want to deploy.
4. Use these basic settings:
   - Runtime: `Python 3`
   - Build Command: `pip install -r requirements_web.txt`
   - Start Command: `gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`

Recommended:

- Instance type: start with a small instance for testing
- Auto deploy: enabled for your chosen branch

### 3. Add environment variables in Render

In `Environment`, add these as **secret** environment variables:

- `FLASK_SECRET_KEY`
- `ANTHROPIC_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_STORAGE_BUCKET=lumen-assets`
- `REQUIRE_DURABLE_STORAGE=true`
- `ALLOW_LOCAL_SESSION_FALLBACK=false`
- `SESSION_COOKIE_SECURE=true`
- `TRUST_PROXY_COUNT=1`
- `VISUALASSISTCAM_DATA_DIR=/var/data/visualassistcam`

Recommended quota and security values:

- `MAX_IMAGE_BYTES=5242880`
- `MAX_REQUEST_BYTES=8388608`
- `DAILY_ANALYSES_LIMIT=20`
- `MONTHLY_ANALYSES_LIMIT=200`
- `DAILY_UPLOAD_LIMIT=30`

Important:

- Do **not** hardcode these into source files for production.
- Keep the service role key only in Render secret env vars. It should never be exposed in client-side code.

### 4. Persistent disk

The app now writes gallery captures, demo images, and history uploads to Supabase Storage when `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_STORAGE_BUCKET` are configured. The Render disk is still useful for logs, temporary files, local development fallback, and migration of older local files.

1. Add a Render Persistent Disk.
2. Mount it at:

```text
/var/data
```

3. Set:

```text
VISUALASSISTCAM_DATA_DIR=/var/data/visualassistcam
```

Without Supabase Storage or a persistent disk fallback, uploaded files and session JSON files can be lost on redeploy/restart. In production, keep `REQUIRE_DURABLE_STORAGE=true` so the app fails loudly instead of silently saving to ephemeral storage.

### 5. Supabase configuration

Before first deploy:

1. Run [supabase_schema.sql](/Users/rishabhpatni/Downloads/Lumen/supabase_schema.sql) in Supabase.
2. Configure Google Auth in Supabase.
3. Add your Render callback URL to Supabase redirect URLs:

```text
https://your-render-service.onrender.com/auth/callback
```

If you later move to your own domain, also add:

```text
https://visualassistcam.com/auth/callback
https://www.visualassistcam.com/auth/callback
```

4. Add the same callback URL to Supabase `Authentication -> URL Configuration`.

### 6. Deploy

1. Trigger the first deploy in Render.
2. Open the Render URL after deployment completes.
3. Verify the flow:
   - `/` shows the landing page
   - Google sign-in redirects correctly
   - `/app` loads after login
   - `/history` and `/settings` are accessible when signed in
   - unauthenticated access redirects back to `/`

### 7. Post-deploy checks

Verify:

- sign-in works with Google
- Supabase history rows are created per user
- class sessions are created in Supabase `class_sessions`
- gallery/demo/history images load after a redeploy or restart
- quota widgets load on the dashboard
- uploaded images persist after a restart
- CSRF-protected POST routes still work from the UI
- logs are being written under the mounted disk path

### 8. Optional production improvements

After initial Render deployment, consider:

- using `gunicorn app:app` as the start command instead of Flask’s built-in server
- moving rate-limit storage from memory to Redis
- migrating any older local-only image files after confirming all active sessions load correctly
- adding uptime monitoring and alerting

## Raspberry Pi deployment

1. Run:

```bash
bash setup_pi.sh
```

2. Add the same Supabase and Anthropic variables to `.env`.
3. Start with:

```bash
bash run_pi.sh
```

4. For internet-facing deployments behind a proxy, set:
   - `SESSION_COOKIE_SECURE=true`
   - `TRUST_PROXY_COUNT=1` or the correct proxy hop count
   - `RATELIMIT_STORAGE_URI=redis://...` instead of in-memory rate-limit storage

## Production notes

- The main app is no longer available at `/`; the protected dashboard lives at `/app`.
- Unauthenticated users are redirected back to `/`.
- The default in-memory rate limiter is fine for a single process but should be replaced with Redis for multi-instance deployments.
- Supabase redirect URLs must exactly match the deployed callback URL.
- The logout button clears both the browser Supabase session and the Flask session.
- The dashboard quota widgets depend on `usage_events` existing in Supabase.
- Render should store all secrets in its environment-variable UI, not in checked-in files.
