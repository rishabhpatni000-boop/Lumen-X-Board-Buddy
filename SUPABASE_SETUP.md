# Supabase Setup

This repository now expects Supabase for Google authentication and persistent per-user analysis history.

## 1. Create a Supabase project

1. Create a new project in Supabase.
2. Copy these values from `Project Settings -> API`:
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`

## 2. Run the SQL schema

Open the Supabase SQL editor and run:

- [supabase_schema.sql](/Users/rishabhpatni/Downloads/Lumen/supabase_schema.sql)

This creates:

- `public.users`
- `public.analysis_history`
- `public.usage_events`
- `public.class_sessions`
- a private `lumen-assets` Supabase Storage bucket for gallery, demo, and history image files
- RLS policies so users can only read and write their own data
- a signup trigger to mirror `auth.users` into `public.users`

## 3. Configure Google Auth in Supabase

In `Authentication -> Providers -> Google`:

1. Enable the Google provider.
2. Add your Google OAuth client ID and client secret.

In Google Cloud Console, configure the authorized redirect URI:

- `https://<your-project-ref>.supabase.co/auth/v1/callback`

That redirect URI points to Supabase, not directly to this Flask app. Supabase then returns the browser to your app callback route.

## 4. Configure app redirect URLs

In `Authentication -> URL Configuration`:

Set your Site URL and add redirect URLs for every environment that should be allowed to complete sign-in.

Typical entries:

- `http://localhost:5050/auth/callback`
- `https://your-domain.com/auth/callback`
- `https://your-pi-domain-or-ip/auth/callback`

If you use HTTPS on Raspberry Pi, include the exact HTTPS callback URL.

## 5. Environment variables

Copy [.env.example](/Users/rishabhpatni/Downloads/Lumen/.env.example) to `.env` and fill in:

- `FLASK_SECRET_KEY`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_STORAGE_BUCKET=lumen-assets`
- `REQUIRE_DURABLE_STORAGE=true` in production
- `ALLOW_LOCAL_SESSION_FALLBACK=false` in production
- `ANTHROPIC_API_KEY`

## 6. How history storage works

- The Flask app creates a secure server session after Supabase login succeeds.
- The signed-in user's Supabase access token is stored in the Flask session.
- History requests to Supabase REST are made with that user token.
- Because RLS is enabled, each user only sees their own `analysis_history` rows.
- Usage and quota tracking are recorded in `usage_events` with the same RLS protections.
- Class metadata is stored in `class_sessions`; image bytes are stored in the private Supabase Storage bucket and served through authenticated Flask routes.
