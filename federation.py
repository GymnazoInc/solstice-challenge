"""Shared-login federation between the Gymnazo apps (Winter Solstice tracker and Ability & Capacity Metrics).

Simple model, NOT single sign-on: each app keeps its own users table and sessions. Only name + PIN credentials
are shared, server to server, over HTTPS, protected by the shared secret FEDERATION_SECRET (header X-Federation-Secret).
This file is identical in both repos. Env vars: FEDERATION_SECRET, PEER_URL (the other app's onrender.com address).
"""
import os, json, time, hmac, re, threading, secrets, urllib.request, urllib.error

SECRET = os.environ.get("FEDERATION_SECRET", "")
PEER = os.environ.get("PEER_URL", "").rstrip("/")
TIMEOUT = float(os.environ.get("FED_TIMEOUT", "3"))
RETRY_SECS = float(os.environ.get("FED_RETRY_SECS", "30"))
LOCK_SECS, MAX_FAILS = 300, 5

def enabled(): return bool(SECRET and PEER)

def peer_call(path, body):
    """POST to the peer. Returns the decoded JSON dict, or None if the peer is down / slow / refused us."""
    if not enabled(): return None
    try:
        req = urllib.request.Request(PEER + path, json.dumps(body).encode(), {"Content-Type": "application/json", "X-Federation-Secret": SECRET, "User-Agent": "gymnazo-federation/1"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r: return json.loads(r.read())
    except Exception: return None

class Fed:
    def __init__(self, db, lock, hp):
        self.db, self.lock, self.hp = db, lock, hp
        self.bad_secret = [0, 0.0]
        if "pin_changed" not in [r[1] for r in db.execute("PRAGMA table_info(users)")]:
            db.execute("ALTER TABLE users ADD COLUMN pin_changed REAL")
        db.execute("CREATE TABLE IF NOT EXISTS fed_outbox(name_key TEXT PRIMARY KEY, name TEXT, salt TEXT, pin_hash TEXT, ts REAL)")
        self._flushing = threading.Lock()
        if enabled(): threading.Thread(target=self._loop, daemon=True).start()

    # ---------- outgoing ----------
    def _loop(self):
        while True:
            time.sleep(RETRY_SECS)
            try: self.flush()
            except Exception: pass
    def queue_pin(self, name, salt, pin_hash, ts, have_lock=False):
        """Admin reset: remember it, then push to the peer in the background (retried until the peer answers)."""
        if not enabled(): return
        q = lambda: self.db.execute("INSERT OR REPLACE INTO fed_outbox VALUES(?,?,?,?,?)", (name.casefold(), name, salt, pin_hash, ts))
        if have_lock: q()
        else:
            with self.lock: q()
        threading.Thread(target=self.flush, daemon=True).start()
    def flush(self):
        if not self._flushing.acquire(blocking=False): return
        try:
            with self.lock: rows = self.db.execute("SELECT name_key,name,salt,pin_hash,ts FROM fed_outbox").fetchall()
            for k, n, s, h, ts in rows:
                r = peer_call("/api/federation/set-pin", {"name": n, "salt": s, "pin_hash": h, "ts": ts})
                if r is not None:  # peer answered (applied or not: no such account / already newer): done
                    with self.lock: self.db.execute("DELETE FROM fed_outbox WHERE name_key=? AND ts=?", (k, ts))
        finally: self._flushing.release()

    # ---------- member-facing flows (called by login / register) ----------
    def login(self, name, pin):
        """('ok', uid, created_here) | ('locked',) | ('bad', peer_unreachable)"""
        key, now = name.casefold(), time.time()
        with self.lock:
            r = self.db.execute("SELECT id,salt,pin_hash,fails,locked_until,COALESCE(pin_changed,created,0) FROM users WHERE name_key=?", (key,)).fetchone()
            if r and r[4] > now: return ("locked",)
            if r and hmac.compare_digest(self.hp(pin, r[1]), r[2]):
                self.db.execute("UPDATE users SET fails=0 WHERE id=?", (r[0],)); return ("ok", r[0], False)
        fed = peer_call("/api/federation/verify", {"name": name, "pin": pin}) if enabled() else None
        with self.lock:
            r = self.db.execute("SELECT id,fails,COALESCE(pin_changed,created,0),locked_until,salt,pin_hash FROM users WHERE name_key=?", (key,)).fetchone()
            if r and hmac.compare_digest(self.hp(pin, r[4]), r[5]):  # the peer's push landed while we were asking: local now matches
                self.db.execute("UPDATE users SET fails=0 WHERE id=?", (r[0],)); return ("ok", r[0], False)
            if fed and fed.get("ok"):
                ts, canon = float(fed.get("pin_changed") or 0), re.sub(r"\s+", " ", str(fed.get("name") or name)).strip()[:24] or name
                salt = secrets.token_hex(16)
                if not r:  # signed up on the other app: create the local account with the same PIN
                    cur = self.db.execute("INSERT INTO users(name,name_key,salt,pin_hash,created,pin_changed) VALUES(?,?,?,?,?,?)", (canon, key, salt, self.hp(pin, salt), now, ts))
                    return ("ok", cur.lastrowid, True)
                if ts > r[2]:  # PIN was reset/changed on the other app more recently: carry it over
                    self.db.execute("UPDATE users SET salt=?,pin_hash=?,pin_changed=?,fails=0,locked_until=0 WHERE id=?", (salt, self.hp(pin, salt), ts, r[0]))
                    return ("ok", r[0], False)
            if r:  # wrong everywhere: count one failure for this name, lock after 5
                f = r[1] + 1
                if f >= MAX_FAILS: self.db.execute("UPDATE users SET fails=0,locked_until=? WHERE id=?", (now + LOCK_SECS, r[0]))
                else: self.db.execute("UPDATE users SET fails=? WHERE id=?", (f, r[0]))
            return ("bad", enabled() and fed is None)
    def register_check(self, name):
        """Before creating a local account: ('ok',) | ('taken',) | ('peer_down',)"""
        if not enabled(): return ("ok",)
        r = peer_call("/api/federation/exists", {"name": name})
        if r is None: return ("peer_down",)   # fail closed: avoids two different people with the same name on the two apps
        return ("taken",) if r.get("exists") else ("ok",)

    # ---------- incoming (peer -> us), all require the shared secret ----------
    def authorized(self, h):
        """Constant-time secret check with its own brute-force lockout. Sends the error itself and returns False."""
        now = time.time()
        if self.bad_secret[1] > now: h.send(429, {"error": "Slow down."}); return False
        given = h.headers.get("X-Federation-Secret", "")
        if not SECRET or not hmac.compare_digest(given.encode(), SECRET.encode()):
            self.bad_secret[0] += 1
            if self.bad_secret[0] >= 8: self.bad_secret[0] = 0; self.bad_secret[1] = now + LOCK_SECS
            h.send(401, {"error": "Not allowed."}); return False
        self.bad_secret[0] = 0; return True
    def handle(self, h, path, b):
        if not self.authorized(h): return
        name = re.sub(r"\s+", " ", str(b.get("name", "")).strip())[:24]; key = name.casefold(); now = time.time()
        with self.lock:
            r = self.db.execute("SELECT id,name,salt,pin_hash,fails,locked_until,COALESCE(pin_changed,created,0) FROM users WHERE name_key=?", (key,)).fetchone() if name else None
            if path == "/api/federation/exists": return h.send(200, {"exists": bool(r)})
            if path == "/api/federation/verify":
                pin = str(b.get("pin", ""))
                if not r or r[5] > now or not re.fullmatch(r"\d{4}", pin): return h.send(200, {"ok": False})   # locked names are not checked
                if hmac.compare_digest(self.hp(pin, r[2]), r[3]):
                    self.db.execute("UPDATE users SET fails=0 WHERE id=?", (r[0],))
                    return h.send(200, {"ok": True, "name": r[1], "pin_changed": r[6]})
                f = r[4] + 1   # a wrong PIN here counts against (and can lock) the name, exactly like a local attempt
                if f >= MAX_FAILS: self.db.execute("UPDATE users SET fails=0,locked_until=? WHERE id=?", (now + LOCK_SECS, r[0]))
                else: self.db.execute("UPDATE users SET fails=? WHERE id=?", (f, r[0]))
                return h.send(200, {"ok": False})
            if path == "/api/federation/set-pin":
                salt, ph, ts = str(b.get("salt", "")), str(b.get("pin_hash", "")), b.get("ts")
                if not (re.fullmatch(r"[0-9a-f]{32}", salt) and re.fullmatch(r"[0-9a-f]{64}", ph) and isinstance(ts, (int, float))) or ts > now + 300:
                    return h.send(400, {"error": "bad request"})
                if not r or ts <= r[6]: return h.send(200, {"applied": False})   # only existing accounts, only newer changes
                self.db.execute("UPDATE users SET salt=?,pin_hash=?,pin_changed=?,fails=0,locked_until=0 WHERE id=?", (salt, ph, ts, r[0]))
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (r[0],))
                return h.send(200, {"applied": True})
        h.send(404, {"error": "nf"})
