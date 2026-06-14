# Production Readiness

## Current Issues Addressed

- Main application was previously exposed directly at `/`.
- Authentication was missing.
- API endpoints were callable without user identity.
- Upload handling was prototype-style and scattered across route files.
- History persistence was incomplete and not user-scoped.
- Quotas were not enforced.
- Request logging and error pages were limited.

## Security Concerns Addressed

- Added Supabase Auth + Google Sign-In
- Added protected route checks for pages and APIs
- Added CSRF protection for unsafe requests
- Added per-IP rate limiting
- Added upload size limits and image validation
- Added structured request/error logging
- Added Supabase RLS policies
- Centralized file writes to reduce path traversal and orphan risk

## Remaining Security Risks

- Flask session storage is cookie-based; move to server-side session backing if you need stronger centralized session revocation.
- SVG storage is preserved for functionality, but any future inline rendering should continue to treat stored SVG carefully.
- `FLASK_SECRET_KEY` must be replaced with a real secret in deployment.
- Redis-backed rate limiting should replace in-memory storage in horizontally scaled environments.

## Scalability Concerns

- Session metadata is still stored on local disk, not in Postgres.
- Duplicate route logic exists in both `app.py` and `app_pi.py`.
- In-memory caches do not share state across instances.
- Local file storage will become limiting for multi-instance deployments.

## Technical Debt

- The main protected UI still lives in one large `templates/index.html` file.
- The web app and legacy PyQt app do not share feature services.
- Session/gallery data remains file-based while user history is database-backed.
- Some frontend behavior still relies on large in-page scripts rather than modular assets.

## Recommended Next Fixes

- Move session/gallery metadata from JSON files to Supabase/Postgres.
- Move local file storage behind an interface compatible with Supabase Storage or S3.
- Split the protected frontend into modular JS/CSS assets.
- Consolidate shared Flask logic into a single factory or shared route module.
- Add automated tests for auth flow, quotas, and history access control.
- Add a background cleanup job for stale temporary/history files if upload volume grows.

## Monitoring Concerns

- Request and event logging exists, but external aggregation is not configured.
- No alerting hooks exist yet for quota spikes, auth failures, or Claude failures.
- No health endpoint exists yet for deployment platforms.

## Performance Concerns

- Claude calls remain the dominant latency source.
- Duplicate request caching reduces some waste but is instance-local.
- Supabase quota/history lookups add network round trips on protected endpoints.

## Summary

The repository has moved from prototype to a safer production-preparation state, but it is not yet fully enterprise-hardened. The biggest remaining gaps are distributed infrastructure choices, deeper frontend modularization, and database-backed replacement of local session/gallery storage.
