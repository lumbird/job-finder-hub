# Job Finder Hub: Roadmap and next steps

*Last updated 27 Oct 2026. The spec is in [product-spec.md](product-spec.md).*

## Where things stand

| Phase | Status |
|---|---|
| 1. Skeleton and login | **Done**, running from a venv. Docker deferred. |
| 2. Jobs | **Done.** Waiting for real Reed/Adzuna keys to prove it against the live APIs. |
| 3. Applications and clusters | Next |
| 4. Emails | Not started |
| 5. Reporting | Not started |
| 6. Notify and extras | Not started |

83 tests pass (`.venv/bin/pytest`).

## The live instance

- **URL:** http://10.0.0.4:8420 (Pi `lummypi`, home network only). Port 8000 is taken by another service on the Pi.
- **Reaching it:** any phone or laptop on the home Wi-Fi can open the URL. Each device signs in with the password. From outside the house it needs Tailscale (see Deployment below).
- **Running as:** user `jamie`, started detached with auto-reload:
  ```bash
  cd ~/claude-rc/job-finder-hub
  setsid nohup .venv/bin/uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8420 \
    --reload --reload-dir app --reload-dir migrations > data/server.log 2>&1 < /dev/null &
  ```
  It **won't survive a Pi reboot**. Rerun the command above, or see Deployment.
- **Stop it:** `pkill -f "app.main:create_app"`
- **Logs:** `data/server.log`
- **Config:** `.env` (`HUB_PUBLIC_ORIGIN=http://10.0.0.4:8420`, `HUB_COOKIE_SECURE=false` because it's plain HTTP).
- **Data:** `data/hub.db` (SQLite), `data/criteria.yaml` once saved, `data/backups/` nightly.

## Waiting on you

1. **Screenshots** of each page after the polish pass, to tweak the styling. Pages: Jobs (empty and with jobs), Profile, Services, a service's connect page, Criteria, Sign-in and security, Login.
2. **API keys** (free):
   - Reed: https://www.reed.co.uk/developers/jobseeker (one key)
   - Adzuna: https://developer.adzuna.com/ (app ID and app key)

   Paste them in Profile → Services. The plug-ins were written from each API's documented format; the first real fetch is the true test.
3. **Your location** in Profile → Criteria. It's blank now, which means the whole UK.
4. Open questions from the spec. Defaults are in use until you decide:
   - Reporting cadence with your work coach (default: per-entry copy plus weekly summary)
   - Notifications via the ADHD secretary's check-ins, or a Telegram channel (default: Telegram)

## Next session: start here

1. Look at the screenshots and fix the styling.
2. Connect the real keys, run "Check now" on Jobs, and fix anything the live APIs return differently from the samples. Record real responses into `tests/fixtures/`, with keys and personal data removed.
3. Start phase 3.

## Phase 3: Applications and clusters

**Data (migration 0003):** `cv_version`, `cluster`, `cluster_job`, `application`, as in the spec's data model.

- **CV versions** (Profile): upload a PDF or DOCX to `data/cvs/`, with a label and created date. Pick a default.
- **Applications:**
  - Create from a job (Save → "Start application"). Status board: Found → Applied → Replied → Interview → Offer → Closed.
  - Manual status changes, follow-up date, notes.
  - Record which CV version and cover note were sent (CV version tracking, a "First" priority).
- **Already-applied guard:** before creating an application, compare the company plus normalised title against applications from the last 6 months. Warn and let you continue, and catch agency reposts (similar title, different company).
- **Clusters (bulk applying):**
  - Group saved jobs by their best-fit criteria profile to start with.
  - A cluster has one cover note template with `{company}` and `{role}` placeholders, and one CV version.
  - Review once, remove jobs, tweak the note, then Approve.
  - Approved, it becomes a submit queue that opens each real apply page in turn. No auto-submit (spec: out of scope).
  - Each job becomes an Application marked Applied once you confirm.
- **Status badges** for the six statuses, meeting WCAG AA in both themes. Check them with the contrast script approach used for the health badges.
- **Tests:** guard matching, cluster note filling, status transitions.

## Phase 4: Emails

- **Gmail:** OAuth 2.0 with read and compose (drafts) scopes only. **Outlook:** msal with Mail.Read and Mail.ReadWrite. Never send permission.
- **OAuth redirect needs an HTTPS origin,** so Tailscale should be in place first (Gmail allows `http://localhost` for testing only).
- Classify messages: job alert, rejection, interview invite, assessment, recruiter outreach, other.
- Parse Indeed, LinkedIn and CV-Library alert emails into jobs. These are new Job Sites plug-ins that read through Emails.
- Link replies to applications and update status. Draft replies are saved as drafts, never sent.
- Tests: recorded sample emails, anonymised.

## Phase 5: Reporting

- `report_entry` table. A Report dropdown on every action (Universal Credit first).
- Entry states: Not reported → Ready to paste → Reported. Copy, copy all, weekly summary, mark reported, undo.
- Log non-application work search too: CV updates, courses, employer research, interview prep.
- Attach confirmation emails as evidence.
- One-tap copy with a toast (clipboard API needs HTTPS, or `localhost`, so this is another reason for Tailscale).

## Phase 6: Notify and extras

- Telegram bot posting to a dedicated channel (ntfy as an alternative): strong matches, replies needing action, follow-ups, closing dates.
- Optional Claude API matching and drafting behind a setting.
- Find an Apprenticeship (GOV.UK API, needs a subscription key), careers page RSS feeds.
- Interviews, contacts, scam filter, then the rest of the spec's Additional features by priority.

## Deployment, when ready

- **Tailscale:** install it (needs root once), then `tailscale serve --bg 8420`. Set `HUB_PUBLIC_ORIGIN=https://lummypi.<tailnet>.ts.net` and remove `HUB_COOKIE_SECURE=false`. That turns on HTTPS, which passkeys, OAuth and the clipboard need, and gives access away from home.
- **Docker Compose** (spec): add a Dockerfile (python:3.12-slim, arm64) and a compose file with the `./data` volume and `.env`. Needs `jamie` in the `docker` group or sudo.
- **Until then,** a user-level systemd service (`systemctl --user`, plus `loginctl enable-linger jamie`) would keep it running across reboots without Docker. Dropping `--reload` would suit everyday use.

## Known gaps and notes

- Python 3.11 on the Pi, not the spec's 3.12. Nothing needs 3.12.
- The pages haven't been checked in a desktop browser by the developer, only on your phone via screenshots.
- Passkeys are untested on a real device (they need HTTPS).
- Job descriptions from the search APIs are snippets. Fetching full details (Reed's `/jobs/{id}`) is a possible addition.
- The Pi has 1.8 GB RAM. Keep memory use modest; Argon2 hashing uses about 64 MB per login.
