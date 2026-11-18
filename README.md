# Photos for Wizard (Immich)

Your own Google Photos replacement on your [Wizard](https://github.com/humanitylabs-org/wizard-os): [Immich](https://immich.app) v3.4.1, unmodified and pinned by digest, with a small Wizard front.

| Service | Image | What it is |
|---|---|---|
| `photos` (main) | `python:3.12-alpine@sha256:1b66…` | Wizard front: page at `/photos/`, agent tools at `/mcp`, passes everything else unchanged to Immich |
| `photos-server` | `ghcr.io/immich-app/immich-server:v3.4.1@sha256:7c48…` | Immich server and web app |
| `photos-ml` | `ghcr.io/immich-app/immich-machine-learning:v3.4.1@sha256:8ade…` | Faces and smart search (CPU) |
| `photos-db` | `ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0@sha256:bcf6…` | Postgres + VectorChord (as pinned by Immich) |
| `photos-redis` | `docker.io/valkey/valkey:9@sha256:4186…` | Valkey (as pinned by Immich) |

Images and environment follow Immich's own [v3.4.1 docker-compose.yml](https://github.com/immich-app/immich/releases/download/v3.4.1/docker-compose.yml). Differences: the database password is random per box (made on first start into a private volume and read through Immich's documented `DB_PASSWORD_FILE`), resource limits, named volumes, and the library under the owner's Files (`Photos/Library`).

## Owner flow
1. Open Photos in Wizard. It is already your library: there is no sign-in (see below).
2. The agent is connected by itself (a narrow Immich API key that stays on the box).
3. Import from Google: Takeout (Google Photos only, .zip, 50 GB) → put every part in `Files/Photos/Import` → **Import now**. Uses [immich-go](https://github.com/simulot/immich-go) v0.32.0, fetched on first use and checked against its release SHA-256.
4. Phone: the official Immich app, server = the app's own Tailscale address (`https://<box>.ts.net:8871`), **Next**, then **Continue**. Nothing to type but the address.
5. Optional: in **Try new features**, turn on **Monthly grandparent print review**. Photos suggests a varied shortlist from the previous month using favorites, faces, semantic matches and print resolution. The owner can adjust it, add a note and explicitly approve the set. Approval never orders, mails or pays for prints.

## No sign-in: Tailscale is the lock
The app's port is served only on the owner's tailnet, so reaching it is the access control. The front makes that true for Immich too, without changing Immich:
- **First start** (`mcp/tools.py`, `_setup_once`): Immich admin sign-up with the placeholder email `owner@photos.wizard` and a random password saved first in the private state volume (`owner.json`, 0600, never shown or logged), one login session, Immich's first-run tour marked done, and the agent's narrow API key.
- **Web** (Wizard screen and full screen): every request that brings no credentials of its own gets that session (`x-immich-user-token`), and page loads get Immich's readable `immich_is_authenticated` cookie, so the web app opens signed in. Sign out just returns to the library.
- **Phone app**: `GET /api/server/features|config` (non-browser clients only) report one OAuth button labelled **Continue** and no password form. `POST /api/oauth/authorize` → `/wizard-sign-in` (redirects straight back to `app.immich:///oauth-callback` with a one-time code) → `POST /api/oauth/callback` (state and PKCE checked) answers with a real Immich login for the account, so each phone gets its own device session. `POST /api/auth/login` with any email/password answers the same way, for older apps.
- Nothing is added for another tailnet user than the owner (`Tailscale-User-Login` vs `WIZARD_OWNER_LOGINS`), a host name the box doesn't serve (DNS rebinding), or a cross-site write from a web page (`Sec-Fetch-Site`/`Origin`); those meet Immich's normal sign-in. Apps on the same box that call the front directly count as the owner, as for the agent tools.
- An Immich that already has an account the front did not create is left alone (status explains); reinstall with data removed to start fresh.

## Agent tools (MCP, box-only)
`photos_status`, `photos_people`, `photos_search`, `photos_print_review` (off-by-default; generate, edit and explicitly approve only, never purchase), `photos_move_to_trash` (preview, then confirmed with the exact count; Immich trash only, never `force`), `photos_restore_from_trash`, `photos_import_status`, `photos_import_start`. See [SKILL.md](SKILL.md).

## Develop
Edit `mcp/front.py`, `mcp/tools.py` or `web/`, then `python3 mcp/inline.py` (regenerates the compose `command` and `app/front.py`), `python3 -m unittest discover -s tests`, `wam verify .`.

## License and attribution
Immich is © Immich contributors, [AGPL-3.0](https://github.com/immich-app/immich/blob/main/LICENSE); used unmodified from its official images. immich-go is © simulot and contributors, AGPL-3.0, downloaded unmodified. This packaging is AGPL-3.0-only (`LICENSE`). The shared Wizard UI kit files in `web/` (`wizard-*.css`, `boot.js`, `wizard-sheet.js`, `standalone*.css`) come from humanitylabs-org/wizard-os under GPL-3.0 (`LICENSE-GPL-3.0-kit`, `appearance-provenance.json`). Immich is not affiliated with this package. Not affiliated with Google.

## Phone backup from Wizard OS for Android

The Phone backup sheet (web/photos.js, tests/photos.test.cjs) is the former view-only Photos app
(humanitylabs-org/wizard-photos-app, by Archimedes for James), merged here so there is one Photos app. The backup
worker stays in Wizard OS for Android (`ai.wizards.os.photos`); this page drives it through the shell's
`wizard-os:photos` relay and hands it this app's own Immich address, so the owner never types a server. In a browser
or the desktop shell there is no relay and the button stays hidden.
