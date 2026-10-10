import http.server, socketserver, sqlite3, json, hashlib, hmac, secrets, os, time, re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
TZ = ZoneInfo("America/Los_Angeles")
START, END = date(2026,10,21), date(2026,12,21)
TOTAL = 62
MILESTONES = [1,7,14,21,30,45,62]
DB = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "solstice.db")
PORT = int(os.environ.get("PORT", 8077))
db = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, name_key TEXT UNIQUE, salt TEXT, pin_hash TEXT, created REAL, fails INTEGER DEFAULT 0, locked_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS checkins(user_id INTEGER, day TEXT, note TEXT, created REAL, PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS wins(user_id INTEGER, milestone INTEGER, created REAL, PRIMARY KEY(user_id, milestone));
""")
LOCK = __import__("threading").Lock()
def today():
    o=os.environ.get('SOL_TEST_TODAY')
    return date.fromisoformat(o) if o else datetime.now(TZ).date()
def hp(pin, salt): return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200000).hex()
def th(t): return hashlib.sha256(t.encode()).hexdigest()
def label(n): return {1:"First check-in",7:"One week strong",14:"Two weeks!",21:"Three weeks!",30:"30 days!",45:"45 days!",62:"Solstice complete!"}[n]

def stats(uid):
    rows = db.execute("SELECT day,note FROM checkins WHERE user_id=?", (uid,)).fetchall()
    days = {date.fromisoformat(r[0]): r[1] or "" for r in rows}
    ds = sorted(days)
    longest = run = 0; prev = None
    for d in ds:
        run = run+1 if prev and (d-prev).days==1 else 1
        longest = max(longest, run); prev = d
    t = today(); cur = 0
    d = t if t in days else t - timedelta(days=1)
    while d in days: cur += 1; d -= timedelta(days=1)
    return {"done":len(days), "current":cur, "longest":longest,
            "days":{k.isoformat():v for k,v in days.items()}}

def user_from(h):
    c = h.headers.get("Cookie","")
    m = re.search(r"sol=([A-Za-z0-9_\-]+)", c)
    if not m: return None
    r = db.execute("SELECT u.id,u.name FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=?", (th(m.group(1)),)).fetchone()
    return r

def me_payload(u):
    s = stats(u[0])
    wins = [r[0] for r in db.execute("SELECT milestone FROM wins WHERE user_id=? ORDER BY milestone",(u[0],))]
    return {"name":u[1], "today":today().isoformat(), "start":START.isoformat(), "end":END.isoformat(),
            "total":TOTAL, "milestones":[{"n":n,"label":label(n)} for n in MILESTONES], "badges":wins, **s}

class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def send(self, code, obj, ctype="application/json", cookie=None):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers(); self.wfile.write(b)
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10000: return {}
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/","/index.html"):
            return self.send(200, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"index.html"),"rb").read(), "text/html; charset=utf-8")
        if p == "/api/me":
            u = user_from(self)
            return self.send(200, me_payload(u)) if u else self.send(401, {"error":"auth"})
        if p == "/api/wins":
            rows = db.execute("SELECT u.name,w.milestone,w.created FROM wins w JOIN users u ON u.id=w.user_id ORDER BY w.created DESC LIMIT 30").fetchall()
            cnt = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return self.send(200, {"members":cnt, "wins":[{"name":r[0],"n":r[1],"label":label(r[1]),"at":r[2]} for r in rows]})
        if p == "/api/leaderboard":
            u = user_from(self)
            rows = []
            for uid,name in db.execute("SELECT id,name FROM users").fetchall():
                st = stats(uid)
                if st["done"]>0: rows.append({"id":uid,"name":name,"done":st["done"],"current":st["current"]})
            rows.sort(key=lambda r:(-r["done"],-r["current"],r["name"].casefold()))
            for i,r in enumerate(rows): r["rank"]=i+1
            mine = next((r for r in rows if u and r["id"]==u[0]), None)
            strip = lambda r:{"rank":r["rank"],"name":r["name"],"done":r["done"],"current":r["current"],"me":bool(u and r["id"]==u[0])}
            return self.send(200, {"total":len(rows),"top":[strip(r) for r in rows[:10]],"me":strip(mine) if mine else None})
        self.send(404, {"error":"nf"})
    def do_POST(self):
        p = self.path.split("?")[0]; b = self.body()
        with LOCK:
            if p == "/api/login": return self.login(b)
            if p == "/api/logout":
                m = re.search(r"sol=([A-Za-z0-9_\-]+)", self.headers.get("Cookie",""))
                if m: db.execute("DELETE FROM sessions WHERE token_hash=?", (th(m.group(1)),))
                return self.send(200, {"ok":1}, cookie="sol=; Path=/; Max-Age=0")
            u = user_from(self)
            if not u: return self.send(401, {"error":"auth"})
            if p == "/api/checkin": return self.checkin(u, b)
        self.send(404, {"error":"nf"})
    def login(self, b):
        name = re.sub(r"\s+"," ", str(b.get("name","")).strip())[:24]
        pin = str(b.get("pin",""))
        if not name or not re.fullmatch(r"\d{4}", pin):
            return self.send(400, {"error":"Enter a display name and a 4-digit PIN."})
        key = name.casefold(); now = time.time()
        r = db.execute("SELECT id,salt,pin_hash,fails,locked_until,name FROM users WHERE name_key=?", (key,)).fetchone()
        if r:
            if r[4] > now: return self.send(429, {"error":"Too many wrong PINs. Try again in a few minutes."})
            if not hmac.compare_digest(hp(pin, r[1]), r[2]):
                f = r[3]+1
                if f >= 5: db.execute("UPDATE users SET fails=0,locked_until=? WHERE id=?", (now+300, r[0]))
                else: db.execute("UPDATE users SET fails=? WHERE id=?", (f, r[0]))
                return self.send(401, {"error":"That name exists and the PIN doesn't match. Pick a different name if you're new."})
            db.execute("UPDATE users SET fails=0 WHERE id=?", (r[0],)); uid, new = r[0], False
        else:
            salt = secrets.token_hex(16)
            cur = db.execute("INSERT INTO users(name,name_key,salt,pin_hash,created) VALUES(?,?,?,?,?)", (name,key,salt,hp(pin,salt),now))
            uid, new = cur.lastrowid, True
        tok = secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (th(tok), uid, now))
        nm = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()[0]
        self.send(200, {"new":new, **me_payload((uid,nm))}, cookie=f"sol={tok}; Path=/; Max-Age=7776000; HttpOnly; SameSite=Lax; Secure")
    def checkin(self, u, b):
        try: d = date.fromisoformat(str(b.get("date")))
        except Exception: return self.send(400, {"error":"bad date"})
        if d < START or d > END or d > today(): return self.send(400, {"error":"That day isn't open for check-in."})
        new = []
        if b.get("done", True):
            note = str(b.get("note",""))[:200]
            db.execute("INSERT INTO checkins VALUES(?,?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET note=excluded.note", (u[0], d.isoformat(), note, time.time()))
            n = stats(u[0])["done"]
            for m in MILESTONES:
                if n >= m and not db.execute("SELECT 1 FROM wins WHERE user_id=? AND milestone=?", (u[0],m)).fetchone():
                    db.execute("INSERT INTO wins VALUES(?,?,?)", (u[0], m, time.time())); new.append(m)
        else:
            db.execute("DELETE FROM checkins WHERE user_id=? AND day=?", (u[0], d.isoformat()))
        self.send(200, {"newBadges":new, **me_payload(u)})

class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True
if __name__ == "__main__":
    S((os.environ.get("HOST", "0.0.0.0"), PORT), H).serve_forever()
