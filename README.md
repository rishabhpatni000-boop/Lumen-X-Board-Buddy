# Lumen

Lumen is an accessibility-focused classroom assistant that captures whiteboard content, reconstructs it into a cleaner AI board, explains lessons in plain language, and stores per-user history behind Google Sign-In.

## Current Flow

Visitor -> Landing Page -> Google Sign-In -> Dashboard -> Analysis Features -> History -> Settings

## Key Features

- protected Flask web application
- Google Sign-In with Supabase Auth
- AI whiteboard reconstruction with Claude
- lesson analysis and follow-up tutoring
- per-user history with Supabase row-level security
- rate limiting, quotas, CSRF, and request logging
- local storage abstraction ready for future cloud storage migration

## Main Routes

- `/`: landing page
- `/app`: protected dashboard and analysis workspace
- `/history`: protected user history
- `/settings`: protected quota/security summary

## Local Setup

1. Create a Python virtual environment.
2. Install [requirements_web.txt](/Users/rishabhpatni/Downloads/Lumen/requirements_web.txt).
3. Copy [.env.example](/Users/rishabhpatni/Downloads/Lumen/.env.example) to `.env`.
4. Configure Supabase and Anthropic values.
5. Start the app with:

```bash
bash run.sh
```

## Required Configuration

- `FLASK_SECRET_KEY`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `ANTHROPIC_API_KEY`

## Important Docs

- [ARCHITECTURE.md](/Users/rishabhpatni/Downloads/Lumen/ARCHITECTURE.md)
- [PRODUCTION_READINESS.md](/Users/rishabhpatni/Downloads/Lumen/PRODUCTION_READINESS.md)
- [SUPABASE_SETUP.md](/Users/rishabhpatni/Downloads/Lumen/SUPABASE_SETUP.md)
- [DEPLOYMENT.md](/Users/rishabhpatni/Downloads/Lumen/DEPLOYMENT.md)

## Raspberry Pi

See [README_PI.md](/Users/rishabhpatni/Downloads/Lumen/README_PI.md) for the Pi-specific setup flow.
