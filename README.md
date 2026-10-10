# Winter Solstice Challenge

A tiny dependency-free Python app (stdlib only) for a 62-day check-in challenge (Oct 21 – Dec 21, 2026, Pacific time).

- Two explicit paths: "I'm new" (POST /api/register, rejects duplicate names) and "I already signed up" (POST /api/login, never creates accounts). Display name + 4-digit PIN (PBKDF2-hashed, lockout after repeated wrong PINs).
- Daily check-in with an optional note, streak tracking, and milestone wins (1, 7, 14, 21, 30, 45, 62).
- A warm "Momentum board" of everyone taking part (API path stays `/api/leaderboard`).
- Optional short notes of encouragement on each activity (members can delete their own; the studio can hide/delete from `/admin`).

## Run
    PORT=8077 DB_PATH=./solstice.db python3 server.py

`SOL_TEST_TODAY=YYYY-MM-DD` overrides "today" for testing. Deploy on Render via `render.yaml` (persistent disk at /var/data).

## Shared login across the Gymnazo apps
The Winter Solstice tracker and the Ability & Capacity Metrics app share name + PIN logins (simple model, not single sign-on): sign up once on either app, then enter the same name and PIN on the other one. Only credentials are shared; members' data stays separate in each app.

- `federation.py` (identical in both repos) talks server to server. Env vars on **both** services: `FEDERATION_SECRET` (same strong random value; never commit it) and `PEER_URL` (the other app's onrender.com address).
- Each app exposes `POST /api/federation/verify`, `/exists` and `/set-pin`, all requiring the `X-Federation-Secret` header (constant-time compare, 401 otherwise, locked for 5 minutes after repeated bad secrets). `verify` answers only `{ok, name, pin_changed}`. Hashes are never returned.
- **Login:** local check first. If the name is unknown locally or the PIN does not match, the peer is asked (3 second timeout). If the peer says OK, the local account is created (if missing) with that PIN, or the local PIN is updated when the peer's `pin_changed` is newer. Otherwise: "That name or PIN doesn't match." and no account is ever created. Wrong PINs count against the name on both sides and lock it after 5 tries; a name locked on the peer is not checked.
- **Register:** the peer is asked whether the name exists; if so the member is told it is taken (log in with the same PIN, or pick a slightly different name). If the peer is unreachable, sign-up pauses with a friendly message (fails closed so two different people cannot end up sharing a name with different PINs). Logging in locally always keeps working.
- **PIN reset:** an admin reset is pushed to the peer (`/api/federation/set-pin`, existing accounts only, newer timestamps only) from a persistent outbox that is retried every 30 seconds until the peer answers, and as a fallback the newer `pin_changed` is pulled lazily at the next login.
- Tests: `python3 tests/test_federation.py` runs both apps against each other (set `SOLSTICE_DIR` / `METRICS_DIR` if the repos are not siblings).
