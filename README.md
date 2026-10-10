# Winter Solstice Challenge

A tiny dependency-free Python app (stdlib only) for a 62-day check-in challenge (Oct 21 – Dec 21, 2026, Pacific time).

- Two explicit paths: "I'm new" (POST /api/register, rejects duplicate names) and "I already signed up" (POST /api/login, never creates accounts). Display name + 4-digit PIN (PBKDF2-hashed, lockout after repeated wrong PINs).
- Daily check-in with an optional note, streak tracking, and milestone wins (1, 7, 14, 21, 30, 45, 62).
- Leaderboard of all participants.

## Run
    PORT=8077 DB_PATH=./solstice.db python3 server.py

`SOL_TEST_TODAY=YYYY-MM-DD` overrides "today" for testing. Deploy on Render via `render.yaml` (persistent disk at /var/data).
