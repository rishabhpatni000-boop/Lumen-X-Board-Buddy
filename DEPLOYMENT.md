# Deployment Instructions

## Current route structure

- `/` -> public landing page
- `/auth/callback` -> completes Supabase login
- `/app` -> protected main application dashboard
- `/camera` -> protected alias to `/app`
- `/history` -> protected per-user history page

Protected API routes include:

- `/analyze`
- `/chat`
- `/save`
- `/calibrate`
- `/update-board`
- `/api/sessions/*`
- `/api/history`
- `/api/images/*`

## Local web deployment

1. Create a virtual environment.
2. Install [requirements_web.txt](/Users/rishabhpatni/Downloads/VisualAssistCam/requirements_web.txt).
3. Copy `.env.example` to `.env` and set:
   - `FLASK_SECRET_KEY`
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `ANTHROPIC_API_KEY`
4. Start the app with:

```bash
bash run.sh
```

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
