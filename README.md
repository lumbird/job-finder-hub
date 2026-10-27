# Job Finder Hub

A self-hosted job search hub for one candidate: finds jobs across many sources, scores them against your CV and criteria, clusters similar jobs to apply in bulk, tracks applications from email replies, and formats work-search entries for Universal Credit.

FastAPI + Jinja2/HTMX + SQLite, built to run on a Raspberry Pi and reached privately over Tailscale. See [docs/product-spec.md](docs/product-spec.md) for the full specification and [docs/ROADMAP.md](docs/ROADMAP.md) for progress and next steps.

## Status

**Phases 1, 2, 3 and 5 (login, jobs, applications and batches, reporting) done, running without Docker for now.** Phase 4 (emails) needs Tailscale first. See the roadmap for what is next.

- First-run setup code (printed to the logs) creates the single login
- Passkey sign-in (fingerprint / screen lock), password fallback with optional authenticator codes
- Sessions: HttpOnly, Secure, SameSite=Strict cookie, 7-day expiry, sign out everywhere
- CSRF token on every form and HTMX request; login slows after 5 failures and locks for 15 minutes after 10
- System / Light / Dark theme rendered server-side (no flash); soft dark greys; status bar colour matches
- Mobile-first layout with a bottom tab bar (Jobs, Applications, Reporting, Profile), installable as a PWA

## Run it (no Docker, no root)

Needs Python 3.11+.

```bash
scripts/run.sh
```

The first run creates `.venv`, installs dependencies, writes generated secrets to `.env` (mode 600) and the database to `data/hub.db`. Look in the output for the **setup code** box, open http://localhost:8000 and enter it with a password of at least 12 characters.

If you restart before setting up, a new code is printed. Once the login exists, no code is issued again.

### From your phone

Passkeys need HTTPS (or `localhost`), so for now:

- **Password over the LAN:** `HOST=0.0.0.0 HUB_COOKIE_SECURE=false scripts/run.sh`, then open `http://<pi-ip>:8000`. Only do this on your home network.
- **Passkeys:** once Tailscale is installed, run `tailscale serve --bg 8000`, set `HUB_PUBLIC_ORIGIN=https://<pi-name>.<tailnet>.ts.net` in `.env`, restart, then add a passkey under Profile → Sign-in and security.

## Tests

```bash
.venv/bin/pytest
```

## Layout

```
app/
  main.py          app factory, security headers, login redirects
  config.py        settings from .env (secrets generated on first run)
  models.py        tables (Alembic migrations in migrations/)
  routes/          auth.py (setup, sign-in, passkeys), pages.py (tabs, profile, theme)
  security/        passwords, sessions, CSRF, throttling, setup code, passkeys, encryption
  templates/       Jinja2 pages
  static/          app.css, app.js, vendored Pico CSS + HTMX, icons, manifest
scripts/           run.sh, make_icons.py
tests/
```

## Differences from the spec (for now)

- Python 3.11 (what the Pi has) rather than 3.12; the code avoids 3.12-only features.
- No Docker Compose yet: runs from a venv as your user. The Dockerfile comes when it's time to deploy.
- Nightly backups arrive with the scheduler in phase 2.
