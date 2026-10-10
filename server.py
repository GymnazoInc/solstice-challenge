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
CREATE TABLE IF NOT EXISTS activities(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, target INTEGER, unit TEXT, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS progress(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, activity_id INTEGER, amount INTEGER, created REAL);
CREATE INDEX IF NOT EXISTS progress_ua ON progress(user_id, activity_id);
CREATE TABLE IF NOT EXISTS user_activities(user_id INTEGER, activity_id INTEGER, created REAL, PRIMARY KEY(user_id, activity_id));
CREATE TABLE IF NOT EXISTS user_picks(user_id INTEGER PRIMARY KEY, saved REAL);
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id INTEGER, user_id INTEGER, body TEXT, created REAL, hidden INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS comments_a ON comments(activity_id, hidden, id);
CREATE TABLE IF NOT EXISTS reactions(comment_id INTEGER, user_id INTEGER, kind TEXT, created REAL, PRIMARY KEY(comment_id, user_id, kind));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
""")
if not db.execute("SELECT 1 FROM meta WHERE k='seeded'").fetchone():
    if db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0:
        for i,(n,t,u) in enumerate([("Jumps",1000,"jumps"),("Minutes of Movement",100,"minutes"),("Squats",50,"squats")]):
            db.execute("INSERT INTO activities(name,target,unit,sort,active) VALUES(?,?,?,?,1)", (n,t,u,i))
    db.execute("INSERT OR REPLACE INTO meta VALUES('seeded','1')")
if "phase" not in [r[1] for r in db.execute("PRAGMA table_info(activities)")]:
    db.execute("ALTER TABLE activities ADD COLUMN phase INTEGER DEFAULT 0")
PHASES = {"1":1,"capacity":1,"2":2,"acquire":2,"3":3,"practice":3}
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_FAILS = [0, 0.0]  # count, locked_until

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

def activities_for(uid):
    out = []
    chosen = {r[0] for r in db.execute("SELECT activity_id FROM user_activities WHERE user_id=?", (uid,))}
    for aid,name,target,unit,phase in db.execute("SELECT id,name,target,unit,COALESCE(phase,0) FROM activities WHERE active=1 ORDER BY sort,id").fetchall():
        tot = db.execute("SELECT COALESCE(SUM(amount),0) FROM progress WHERE user_id=? AND activity_id=?", (uid,aid)).fetchone()[0]
        tot = min(tot, target)
        out.append({"id":aid,"name":name,"target":target,"unit":unit,"phase":phase,"total":tot,"pct":int(tot*100//target) if target else 0,"done":tot>=target,"chosen":aid in chosen,"notes":db.execute("SELECT COUNT(*) FROM comments WHERE activity_id=? AND hidden=0",(aid,)).fetchone()[0]})
    return out

def me_payload(u):
    s = stats(u[0])
    acts = activities_for(u[0])
    wins = [r[0] for r in db.execute("SELECT milestone FROM wins WHERE user_id=? ORDER BY milestone",(u[0],))]
    return {"name":u[1], "today":today().isoformat(), "start":START.isoformat(), "end":END.isoformat(),
            "total":TOTAL, "milestones":[{"n":n,"label":label(n)} for n in MILESTONES], "badges":wins, "activities":acts, "activitiesDone":sum(1 for a in acts if a["done"]),"picksSaved":bool(db.execute("SELECT 1 FROM user_picks WHERE user_id=?", (u[0],)).fetchone()), **s}

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
        if p == "/admin":
            return self.send(200, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"admin.html"),"rb").read(), "text/html; charset=utf-8")
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
                if st["done"]>0: rows.append({"id":uid,"name":name,"done":st["done"],"current":st["current"],"acts":sum(1 for a in activities_for(uid) if a["done"])})
            rows.sort(key=lambda r:(-r["done"],-r["current"],r["name"].casefold()))
            for i,r in enumerate(rows): r["rank"]=i+1
            mine = next((r for r in rows if u and r["id"]==u[0]), None)
            strip = lambda r:{"rank":r["rank"],"name":r["name"],"done":r["done"],"current":r["current"],"acts":r["acts"],"me":bool(u and r["id"]==u[0])}
            return self.send(200, {"total":len(rows),"top":[strip(r) for r in rows[:10]],"me":strip(mine) if mine else None})
        self.send(404, {"error":"nf"})
    def do_POST(self):
        p = self.path.split("?")[0]; b = self.body()
        with LOCK:
            if p == "/api/login": return self.login(b)
            if p == "/api/register": return self.register(b)
            if p == "/api/logout":
                m = re.search(r"sol=([A-Za-z0-9_\-]+)", self.headers.get("Cookie",""))
                if m: db.execute("DELETE FROM sessions WHERE token_hash=?", (th(m.group(1)),))
                return self.send(200, {"ok":1}, cookie="sol=; Path=/; Max-Age=0")
            if p.startswith("/api/admin/"): return self.admin(p, b)
            u = user_from(self)
            if not u: return self.send(401, {"error":"auth"})
            if p == "/api/comments/list": return self.comments_list(u, b)
            if p == "/api/comments": return self.comment_post(u, b)
            if p == "/api/comments/delete": return self.comment_delete(u, b)
            if p == "/api/comments/react": return self.comment_react(u, b)
            if p == "/api/picks": return self.save_picks(u, b)
            if p == "/api/progress": return self.log_progress(u, b)
            if p == "/api/progress/undo": return self.undo_progress(u, b)
            if p == "/api/checkin": return self.checkin(u, b)
        self.send(404, {"error":"nf"})
    def _parse(self, b):
        name = re.sub(r"\s+"," ", str(b.get("name","")).strip())[:24]
        pin = str(b.get("pin",""))
        if not name or not re.fullmatch(r"\d{4}", pin):
            self.send(400, {"error":"Please type your name and a 4-digit PIN (numbers only)."}); return None
        return name, pin
    def _session(self, uid, new):
        now = time.time(); tok = secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (th(tok), uid, now))
        nm = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()[0]
        self.send(200, {"new":new, **me_payload((uid,nm))}, cookie=f"sol={tok}; Path=/; Max-Age=7776000; HttpOnly; SameSite=Lax; Secure")
    def login(self, b):
        # Log in only: never creates an account.
        p = self._parse(b)
        if not p: return
        name, pin = p; key = name.casefold(); now = time.time()
        bad = {"error":"That name or PIN doesn't match. Please try again."}
        r = db.execute("SELECT id,salt,pin_hash,fails,locked_until FROM users WHERE name_key=?", (key,)).fetchone()
        if not r: return self.send(401, bad)
        if r[4] > now: return self.send(429, {"error":"Too many tries. Please wait 5 minutes, then try again. Forgot your PIN? Ask the studio."})
        if not hmac.compare_digest(hp(pin, r[1]), r[2]):
            f = r[3]+1
            if f >= 5: db.execute("UPDATE users SET fails=0,locked_until=? WHERE id=?", (now+300, r[0]))
            else: db.execute("UPDATE users SET fails=? WHERE id=?", (f, r[0]))
            return self.send(401, bad)
        db.execute("UPDATE users SET fails=0 WHERE id=?", (r[0],))
        self._session(r[0], False)
    def register(self, b):
        p = self._parse(b)
        if not p: return
        name, pin = p; key = name.casefold()
        if db.execute("SELECT 1 FROM users WHERE name_key=?", (key,)).fetchone():
            return self.send(409, {"error":"That name is already taken. If that's you, tap \"Log in\". If not, please pick a slightly different name (like adding your last initial)."})
        salt = secrets.token_hex(16)
        cur = db.execute("INSERT INTO users(name,name_key,salt,pin_hash,created) VALUES(?,?,?,?,?)", (name,key,salt,hp(pin,salt),time.time()))
        self._session(cur.lastrowid, True)
    def _clist(self, u, aid, offset):
        tot = db.execute("SELECT COUNT(*) FROM comments WHERE activity_id=? AND hidden=0", (aid,)).fetchone()[0]
        rows = db.execute("SELECT c.id,u.name,c.body,c.created,c.user_id FROM comments c JOIN users u ON u.id=c.user_id WHERE c.activity_id=? AND c.hidden=0 ORDER BY c.id DESC LIMIT 5 OFFSET ?", (aid,offset)).fetchall()
        def rx(cid):
            c = {"heart":0,"thumb":0}; mine = []
            for k,n,m in db.execute("SELECT kind,COUNT(*),SUM(user_id=?) FROM reactions WHERE comment_id=? GROUP BY kind", (u[0],cid)):
                if k in c: c[k] = n
                if m: mine.append(k)
            return c, mine
        out = []
        for r in rows:
            c, mine = rx(r[0])
            out.append({"id":r[0],"name":r[1],"body":r[2],"at":r[3],"mine":r[4]==u[0],"hearts":c["heart"],"thumbs":c["thumb"],"myReactions":mine})
        return {"activity_id":aid,"total":tot,"offset":offset,"comments":out}
    def comments_list(self, u, b):
        try: aid = int(b.get("activity_id")); off = max(0, int(b.get("offset", 0)))
        except Exception: return self.send(400, {"error":"bad request"})
        self.send(200, self._clist(u, aid, off))
    def comment_post(self, u, b):
        try: aid = int(b.get("activity_id"))
        except Exception: return self.send(400, {"error":"bad request"})
        if not db.execute("SELECT 1 FROM activities WHERE id=? AND active=1", (aid,)).fetchone():
            return self.send(404, {"error":"That activity isn't available."})
        body = re.sub(r"<[^>]*>", "", str(b.get("body",""))); body = re.sub(r"\s+", " ", body).strip()[:500]
        if not body: return self.send(400, {"error":"Please write a few words first."})
        now = time.time()
        if db.execute("SELECT COUNT(*) FROM comments WHERE user_id=? AND created>?", (u[0], now-86400)).fetchone()[0] >= 20:
            return self.send(429, {"error":"You've shared lots of encouragement today. Thank you! Please come back tomorrow."})
        last = db.execute("SELECT MAX(created) FROM comments WHERE user_id=? AND activity_id=?", (u[0],aid)).fetchone()[0]
        if last and now - last < 10:
            return self.send(429, {"error":"Nice! Please wait a few seconds before adding another note."})
        db.execute("INSERT INTO comments(activity_id,user_id,body,created,hidden) VALUES(?,?,?,?,0)", (aid,u[0],body,now))
        self.send(200, self._clist(u, aid, 0))
    def comment_react(self, u, b):
        kind = b.get("kind")
        if kind not in ("heart","thumb"): return self.send(400, {"error":"Only a heart or a thumbs up is allowed."})
        try: cid = int(b.get("id"))
        except Exception: return self.send(400, {"error":"bad request"})
        r = db.execute("SELECT activity_id FROM comments WHERE id=? AND hidden=0", (cid,)).fetchone()
        if not r: return self.send(404, {"error":"That note isn't available."})
        if db.execute("SELECT 1 FROM reactions WHERE comment_id=? AND user_id=? AND kind=?", (cid,u[0],kind)).fetchone():
            db.execute("DELETE FROM reactions WHERE comment_id=? AND user_id=? AND kind=?", (cid,u[0],kind))
        else:
            db.execute("INSERT INTO reactions VALUES(?,?,?,?)", (cid,u[0],kind,time.time()))
        try: off = max(0, int(b.get("offset", 0)))
        except Exception: off = 0
        self.send(200, self._clist(u, r[0], 0))
    def comment_delete(self, u, b):
        try: cid = int(b.get("id"))
        except Exception: return self.send(400, {"error":"bad request"})
        r = db.execute("SELECT activity_id FROM comments WHERE id=? AND user_id=?", (cid,u[0])).fetchone()
        if not r: return self.send(404, {"error":"That note isn't yours to delete."})
        db.execute("DELETE FROM comments WHERE id=?", (cid,)); db.execute("DELETE FROM reactions WHERE comment_id=?", (cid,))
        self.send(200, self._clist(u, r[0], 0))
    def save_picks(self, u, b):
        ids = b.get("ids")
        if not isinstance(ids, list): return self.send(400, {"error":"bad picks"})
        valid = {r[0] for r in db.execute("SELECT id FROM activities WHERE active=1")}
        keep = set()
        for x in ids:
            try:
                if int(x) in valid: keep.add(int(x))
            except Exception: pass
        # only the selection changes; logged progress rows are never touched
        db.execute("DELETE FROM user_activities WHERE user_id=?", (u[0],))
        for a in keep: db.execute("INSERT INTO user_activities VALUES(?,?,?)", (u[0],a,time.time()))
        db.execute("INSERT OR REPLACE INTO user_picks VALUES(?,?)", (u[0], time.time()))
        self.send(200, me_payload(u))
    def log_progress(self, u, b):
        try: aid = int(b.get("activity_id")); amt = int(b.get("amount"))
        except Exception: return self.send(400, {"error":"Please type a whole number."})
        a = db.execute("SELECT target FROM activities WHERE id=? AND active=1", (aid,)).fetchone()
        if not a: return self.send(404, {"error":"That activity isn't available."})
        if amt < 1 or amt > 1000000: return self.send(400, {"error":"Please type a number bigger than 0."})
        tot = db.execute("SELECT COALESCE(SUM(amount),0) FROM progress WHERE user_id=? AND activity_id=?", (u[0],aid)).fetchone()[0]
        room = a[0] - tot
        if room <= 0: return self.send(400, {"error":"You already finished this one. Amazing!"})
        amt = min(amt, room)
        db.execute("INSERT INTO progress(user_id,activity_id,amount,created) VALUES(?,?,?,?)", (u[0],aid,amt,time.time()))
        self.send(200, {"added":amt, **me_payload(u)})
    def undo_progress(self, u, b):
        try: aid = int(b.get("activity_id"))
        except Exception: return self.send(400, {"error":"bad activity"})
        r = db.execute("SELECT id FROM progress WHERE user_id=? AND activity_id=? ORDER BY id DESC LIMIT 1", (u[0],aid)).fetchone()
        if not r: return self.send(400, {"error":"Nothing to undo."})
        db.execute("DELETE FROM progress WHERE id=?", (r[0],))
        self.send(200, me_payload(u))
    def admin(self, p, b):
        if not ADMIN_PASSWORD: return self.send(403, {"error":"Admin is not set up."})
        now = time.time()
        if ADMIN_FAILS[1] > now: return self.send(429, {"error":"Too many wrong passwords. Wait a few minutes."})
        given = self.headers.get("X-Admin-Password","")
        if not hmac.compare_digest(given.encode(), ADMIN_PASSWORD.encode()):
            ADMIN_FAILS[0] += 1
            if ADMIN_FAILS[0] >= 8: ADMIN_FAILS[0] = 0; ADMIN_FAILS[1] = now + 300
            return self.send(401, {"error":"Wrong password."})
        ADMIN_FAILS[0] = 0
        def lst():
            rows = db.execute("SELECT id,name,target,unit,sort,active,COALESCE(phase,0) FROM activities ORDER BY sort,id").fetchall()
            return {"activities":[{"id":r[0],"name":r[1],"target":r[2],"unit":r[3],"sort":r[4],"active":bool(r[5]),"phase":r[6],
                    "finished":db.execute("SELECT COUNT(*) FROM (SELECT user_id FROM progress WHERE activity_id=? GROUP BY user_id HAVING SUM(amount)>=?)", (r[0],r[2])).fetchone()[0]} for r in rows]}
        if p == "/api/admin/list": return self.send(200, lst())
        if p == "/api/admin/save":
            name = str(b.get("name","")).strip()[:60]; unit = str(b.get("unit","")).strip()[:30]
            try: target = int(b.get("target"))
            except Exception: target = 0
            if not name or not unit or target < 1 or target > 1000000: return self.send(400, {"error":"Please give a name, a unit, and a target between 1 and 1,000,000."})
            active = 1 if b.get("active", True) else 0
            try: phase = int(b.get("phase", 0))
            except Exception: phase = 0
            if phase not in (0,1,2,3): phase = 0
            if b.get("id"):
                db.execute("UPDATE activities SET name=?,target=?,unit=?,active=?,phase=? WHERE id=?", (name,target,unit,active,phase,int(b["id"])))
            else:
                mx = db.execute("SELECT COALESCE(MAX(sort),-1)+1 FROM activities").fetchone()[0]
                db.execute("INSERT INTO activities(name,target,unit,sort,active,phase) VALUES(?,?,?,?,?,?)", (name,target,unit,mx,active,phase))
            return self.send(200, lst())
        if p == "/api/admin/bulk":
            text = str(b.get("text",""))[:100000]; dry = bool(b.get("dry"))
            seen = {r[0].casefold() for r in db.execute("SELECT name FROM activities WHERE active=1")}
            added, skipped, bad = [], [], []
            for n, line in enumerate(text.splitlines(), 1):
                if not line.strip(): continue
                parts = [x.strip() for x in line.split("|")]
                name = parts[0][:60]
                try: target = int(parts[1].replace(",","")) if len(parts) > 1 else 0
                except Exception: target = 0
                ph = 0
                if len(parts) > 3 and parts[3]:
                    ph = PHASES.get(parts[3].casefold(), -1)
                if len(parts) > 4 or ph < 0 or not name or target < 1 or target > 1000000:
                    bad.append({"line":n,"text":line.strip()[:80]}); continue
                unit = (parts[2] if len(parts) > 2 and parts[2] else "times")[:30]
                if name.casefold() in seen: skipped.append({"line":n,"name":name}); continue
                seen.add(name.casefold()); added.append({"line":n,"name":name,"target":target,"unit":unit,"phase":ph})
            if not dry:
                for a in added:
                    mx = db.execute("SELECT COALESCE(MAX(sort),-1)+1 FROM activities").fetchone()[0]
                    db.execute("INSERT INTO activities(name,target,unit,sort,active,phase) VALUES(?,?,?,?,1,?)", (a["name"],a["target"],a["unit"],mx,a["phase"]))
            return self.send(200, {"dry":dry,"added":added,"skipped":skipped,"bad":bad, **lst()})
        if p == "/api/admin/comments":
            try: aid = int(b.get("activity_id") or 0)
            except Exception: aid = 0
            q = "SELECT c.id,c.activity_id,a.name,u.name,c.body,c.created,c.hidden FROM comments c JOIN activities a ON a.id=c.activity_id JOIN users u ON u.id=c.user_id"
            rows = db.execute(q + (" WHERE c.activity_id=?" if aid else "") + " ORDER BY c.id DESC LIMIT 200", ((aid,) if aid else ())).fetchall()
            tot = db.execute("SELECT COUNT(*), COALESCE(SUM(hidden),0) FROM comments").fetchone()
            return self.send(200, {"total":tot[0],"hidden":tot[1],"comments":[{"id":r[0],"activity_id":r[1],"activity":r[2],"author":r[3],"body":r[4],"at":r[5],"hidden":bool(r[6])} for r in rows]})
        if p == "/api/admin/comments/hide":
            try: db.execute("UPDATE comments SET hidden=? WHERE id=?", (1 if b.get("hidden", True) else 0, int(b.get("id"))))
            except Exception: return self.send(400, {"error":"bad request"})
            return self.send(200, {"ok":1})
        if p == "/api/admin/comments/delete":
            try: cid = int(b.get("id")); db.execute("DELETE FROM comments WHERE id=?", (cid,)); db.execute("DELETE FROM reactions WHERE comment_id=?", (cid,))
            except Exception: return self.send(400, {"error":"bad request"})
            return self.send(200, {"ok":1})
        if p == "/api/admin/reorder":
            ids = b.get("ids", [])
            if isinstance(ids, list):
                for i,x in enumerate(ids):
                    try: db.execute("UPDATE activities SET sort=? WHERE id=?", (i,int(x)))
                    except Exception: pass
            return self.send(200, lst())
        self.send(404, {"error":"nf"})
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
