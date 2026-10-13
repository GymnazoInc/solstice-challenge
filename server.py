import federation, csv, io, http.server, socketserver, sqlite3, json, hashlib, hmac, secrets, os, time, re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
TZ = ZoneInfo("America/Los_Angeles")
START, END = date(2026,10,21), date(2026,12,21)
TOTAL = 62
MILESTONES = [1]
DB = os.environ.get("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "solstice.db")
PORT = int(os.environ.get("PORT", 8077))
BETA_DB = os.environ.get("BETA_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(DB)), "solstice-beta.db")
BETA_START = date.fromisoformat(os.environ.get("BETA_START", "2026-10-12"))
import threading
_TL = threading.local()
class _DBProxy:
    """Routes queries to the main DB, or to the separate beta DB for /beta requests (per thread)."""
    def __getattr__(self, k): return getattr(getattr(_TL, "conn", None) or MAIN, k)
def is_beta(): return getattr(_TL, "conn", None) is BETA
def init_schema(db):
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
MAIN = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
db = MAIN
init_schema(MAIN)
if not db.execute("SELECT 1 FROM meta WHERE k='seeded'").fetchone():
    if db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0:
        for i,(n,t,u) in enumerate([("Jumps",1000,"jumps"),("Minutes of Movement",100,"minutes"),("Squats",50,"squats")]):
            db.execute("INSERT INTO activities(name,target,unit,sort,active) VALUES(?,?,?,?,1)", (n,t,u,i))
    db.execute("INSERT OR REPLACE INTO meta VALUES('seeded','1')")
if "phase" not in [r[1] for r in db.execute("PRAGMA table_info(activities)")]:
    db.execute("ALTER TABLE activities ADD COLUMN phase INTEGER DEFAULT 0")
_cols = [r[1] for r in db.execute("PRAGMA table_info(activities)")]
if "kind" not in _cols: db.execute("ALTER TABLE activities ADD COLUMN kind TEXT DEFAULT 'chunked'")
if "record_type" not in _cols: db.execute("ALTER TABLE activities ADD COLUMN record_type TEXT DEFAULT 'none'")
db.execute("CREATE TABLE IF NOT EXISTS results(user_id INTEGER, activity_id INTEGER, seconds INTEGER, weight REAL, wunit TEXT, created REAL, PRIMARY KEY(user_id, activity_id))")
if "repeat_count" not in _cols: db.execute("ALTER TABLE activities ADD COLUMN repeat_count INTEGER DEFAULT 3")
db.execute("CREATE TABLE IF NOT EXISTS repeats(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, activity_id INTEGER, day TEXT, note TEXT, created REAL)")
db.execute("CREATE INDEX IF NOT EXISTS repeats_ua ON repeats(user_id, activity_id)")
if "pin_changed" not in [r[1] for r in db.execute("PRAGMA table_info(users)")]: db.execute("ALTER TABLE users ADD COLUMN pin_changed REAL")
for c_ in (MAIN,):
    c_.execute("CREATE TABLE IF NOT EXISTS badges(user_id INTEGER, key TEXT, created REAL, PRIMARY KEY(user_id, key))")
BETA = sqlite3.connect(BETA_DB, check_same_thread=False, isolation_level=None)
init_schema(BETA)
def migrate(c):
    cols = [r[1] for r in c.execute("PRAGMA table_info(activities)")]
    for col, ddl in (("phase","INTEGER DEFAULT 0"),("kind","TEXT DEFAULT 'chunked'"),("record_type","TEXT DEFAULT 'none'"),("repeat_count","INTEGER DEFAULT 3")):
        if col not in cols: c.execute("ALTER TABLE activities ADD COLUMN %s %s" % (col, ddl))
    c.execute("CREATE TABLE IF NOT EXISTS results(user_id INTEGER, activity_id INTEGER, seconds INTEGER, weight REAL, wunit TEXT, created REAL, PRIMARY KEY(user_id, activity_id))")
    c.execute("CREATE TABLE IF NOT EXISTS repeats(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, activity_id INTEGER, day TEXT, note TEXT, created REAL)")
    if "pin_changed" not in [r[1] for r in c.execute("PRAGMA table_info(users)")]: c.execute("ALTER TABLE users ADD COLUMN pin_changed REAL")
migrate(BETA)
def migrate2(c):
    c.execute("CREATE TABLE IF NOT EXISTS badges(user_id INTEGER, key TEXT, created REAL, PRIMARY KEY(user_id, key))")
    c.execute("CREATE TABLE IF NOT EXISTS admin_audit(ts REAL, user_id INTEGER, action TEXT, detail TEXT)")
    if "decimals" not in [r[1] for r in c.execute("PRAGMA table_info(activities)")]: c.execute("ALTER TABLE activities ADD COLUMN decimals INTEGER DEFAULT 0")
    # carry over old first-check-in wins once
    if not c.execute("SELECT 1 FROM meta WHERE k='badges_v1'").fetchone():
        c.execute("INSERT OR IGNORE INTO badges SELECT user_id,'first',created FROM wins WHERE milestone=1")
        c.execute("INSERT OR REPLACE INTO meta VALUES('badges_v1','1')")
    # Loaded Carry: feet -> miles, stored in hundredths of a mile (target 100 = 1.00 mi)
    for aid, unit, tgt in c.execute("SELECT id,unit,target FROM activities WHERE lower(name) LIKE '%loaded carry%' AND COALESCE(decimals,0)=0").fetchall():
        if str(unit).lower() in ("ft","feet"):
            for pid, amt in c.execute("SELECT id,amount FROM progress WHERE activity_id=?", (aid,)).fetchall():
                c.execute("UPDATE progress SET amount=? WHERE id=?", (max(1, round(amt * 100 / 5280)), pid))
        c.execute("UPDATE activities SET unit='miles',target=100,decimals=2 WHERE id=?", (aid,))
migrate2(MAIN); migrate2(BETA)
BETA_TABLES = ("admin_audit","badges","users","sessions","checkins","wins","progress","user_activities","user_picks","comments","reactions","results","repeats")
def beta_sync_activities():
    """Beta uses the same activity list as the real challenge (copied from the main DB, same ids)."""
    rows = MAIN.execute("SELECT id,name,target,unit,sort,active,phase,kind,record_type,repeat_count,decimals FROM activities").fetchall()
    BETA.execute("DELETE FROM activities")
    BETA.executemany("INSERT INTO activities(id,name,target,unit,sort,active,phase,kind,record_type,repeat_count,decimals) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
def beta_wipe():
    for t in BETA_TABLES: BETA.execute("DELETE FROM %s" % t)
    beta_sync_activities()
beta_sync_activities()
KINDS = ("chunked","once","repeat"); RECS = ("time","weight","both","none")
PHASES = {"1":1,"capacity":1,"2":2,"acquire":2,"3":3,"practice":3}
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_FAILS = [0, 0.0]  # count, locked_until

LOCK = __import__("threading").Lock()
def today():
    o=os.environ.get('SOL_TEST_TODAY')
    return date.fromisoformat(o) if o else datetime.now(TZ).date()
def start_end(): return (BETA_START, BETA_START + timedelta(days=TOTAL-1)) if is_beta() else (START, END)
CK = lambda: "solb" if is_beta() else "sol"
def hp(pin, salt): return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200000).hex()
def th(t): return hashlib.sha256(t.encode()).hexdigest()
FED = federation.Fed(MAIN, LOCK, hp)
db = _DBProxy()
# Badges: add more by appending to BADGES. kind "first" = first check-in; "phase" = every active activity in that bucket done;
# "names" = every activity whose name contains one of the listed phrases (case-insensitive) done.
BADGES = [
    {"key":"first","label":"First check-in","icon":"❄️","desc":"Checked in for the first time","kind":"first"},
    {"key":"capacity","label":"Capacity Badge","icon":"💪","desc":"Finished every Capacity activity","kind":"phase","phase":1},
    {"key":"acquire","label":"Acquire Badge","icon":"🏔️","desc":"Finished every Acquire activity","kind":"phase","phase":2},
    {"key":"practice","label":"Practice Badge","icon":"🌙","desc":"Finished every Practice activity","kind":"phase","phase":3},
    {"key":"selflove","label":"Self Love Badge","icon":"💖","desc":"Cold plunge, 10 hugs, TrueStretch, journaling and 100 Supine Get-ups all done","kind":"names",
     "names":[["ocean plunge","cold shower"],["hugs"],["truestretch"],["journal"],["supine get"]]},
    {"key":"row","label":"Row, Row, Row Your Boat","icon":"🚣","desc":"Finished the 500m, 1,000m and 2,000m rows","kind":"names",
     "names":[["500m row"],["1,000m row","1000m row"],["2,000m row","2000m row"]]},
    {"key":"muscle","label":"Mighty Muscles","icon":"💪","desc":"Finished the Kettlebell Squat, ViPR Press and FitFighter Slam","kind":"names",
     "names":[["kettlebell squat"],["overhead press"],["overhead slam"]]},
    {"key":"miles","label":"Miles & Smiles","icon":"🥾","desc":"Finished the Gymnazo Loop, Gymnazo Mile, a hike and the Loaded Carry mile","kind":"names",
     "names":[["gymnazo loop"],["gymnazo mile"],["hike"],["loaded carry"]]},
    {"key":"rock","label":"Stability Master","icon":"🪨","desc":"Finished the Plank, Wall Sit, Single Leg Balance and Hang","kind":"names",
     "names":[["plank"],["wall sit"],["single leg balance"],["min hang"]]},
    {"key":"circle","label":"Spherical Flow","icon":"🌀","desc":"Finished Rope Flow, Mace 360/Plate Halo, Lunge Matrix and TrueStretch","kind":"names",
     "names":[["rope flow"],["mace","plate halo"],["lunge matrix"],["truestretch"]]},
    {"key":"legs","label":"Leg Day Legend","icon":"🦵","desc":"Finished every leg activity","kind":"names",
     "names":[["kettlebell squat"],["wall sit"],["single leg balance"],["jumps or calf raises","calf raise"],["box jump","box step"],["deep squat"],["lunge matrix"]]},
    {"key":"arms","label":"Strong Arms","icon":"🥊","desc":"Finished every arm activity","kind":"names",
     "names":[["overhead slam"],["overhead press"],["min hang"],["medball throw","med ball throw","medicine ball throw"],["mace","plate halo"]]},
    {"key":"count10","label":"Snowball Rolling","icon":"⛄","desc":"Finished 10 activities","prize":"You earned a raffle entry!","kind":"count","n":10},
    {"key":"count20","label":"Shining Star","icon":"🌟","desc":"Finished 20 activities","prize":"You earned a second raffle entry!","kind":"count","n":20},
    {"key":"count30","label":"Solstice Champion","icon":"🏆","desc":"Finished all 30 activities","prize":"You earned a 1-month membership upgrade and a free shirt!","kind":"count","n":30},
    {"key":"timed","label":"Time Tracker","icon":"⏱️","desc":"Complete 3 timed activities","kind":"timed","n":3},
]
def is_timed(a):
    """Timed = measured in minutes (unit 'min…', or '10min'/'(2m+)' in the name), or a one-time effort where a time was recorded."""
    n = a["name"].casefold()
    if str(a.get("unit","")).casefold().startswith("min") or re.search(r"\d+\s*min", n) or re.search(r"\(\d+m\+\)", n): return True
    return a.get("kind") == "once" and bool((a.get("result") or {}).get("seconds"))
def timed_done(acts): return len({a["id"] for a in acts if a["done"] and is_timed(a)})
def badge_hint(b, acts):
    """Progress hint for a locked badge (only for counted badges)."""
    if b["kind"] == "timed": return "%d of %d timed activities done" % (min(timed_done(acts), b.get("n",1)), b.get("n",1))
    if b["kind"] == "count": return "%d of %d activities done" % (min(sum(1 for a in acts if a["done"]), b["n"]), b["n"])
    return ""
BADGE = {b["key"]:b for b in BADGES}
def badge_pub(b): return {"key":b["key"],"label":b["label"],"icon":b["icon"],"desc":b["desc"],"prize":b.get("prize","")}
def badge_ok(b, uid, acts):
    if b["kind"] == "first": return db.execute("SELECT 1 FROM checkins WHERE user_id=? LIMIT 1", (uid,)).fetchone() is not None
    if b["kind"] == "phase":
        L = [a for a in acts if a["phase"] == b["phase"]]
        return bool(L) and all(a["done"] for a in L)
    if b["kind"] == "count": return sum(1 for a in acts if a["done"]) >= b["n"]
    if b["kind"] == "timed": return timed_done(acts) >= b.get("n", 1)
    if b["kind"] == "names":
        for alts in b["names"]:
            L = [a for a in acts if any(x in a["name"].casefold() for x in alts)]
            if not L or not any(a["done"] for a in L): return False
        return True
    return False
def sync_badges(uid):
    """Award newly earned badges (and drop ones no longer earned, e.g. after an undo). Returns new badge dicts."""
    acts = activities_for(uid); have = {r[0] for r in db.execute("SELECT key FROM badges WHERE user_id=?", (uid,))}; new = []
    for b in BADGES:
        ok = badge_ok(b, uid, acts)
        if ok and b["key"] not in have:
            db.execute("INSERT OR IGNORE INTO badges VALUES(?,?,?)", (uid, b["key"], time.time())); new.append(badge_pub(b))
        elif not ok and b["key"] in have: db.execute("DELETE FROM badges WHERE user_id=? AND key=?", (uid, b["key"]))
    return new
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
    m = re.search(r"(?:^|;)\s*%s=([A-Za-z0-9_\-]+)" % CK(), c)
    if not m: return None
    r = db.execute("SELECT u.id,u.name FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=?", (th(m.group(1)),)).fetchone()
    return r

def activities_for(uid):
    out = []
    chosen = {r[0] for r in db.execute("SELECT activity_id FROM user_activities WHERE user_id=?", (uid,))}
    res = {r[0]:r[1:] for r in db.execute("SELECT activity_id,seconds,weight,wunit FROM results WHERE user_id=?", (uid,))}
    reps = {}
    for aid_,day_,note_ in db.execute("SELECT activity_id,day,note FROM repeats WHERE user_id=? ORDER BY id", (uid,)): reps.setdefault(aid_, []).append({"day":day_,"note":note_ or ""})
    for aid,name,target,unit,phase,kind,rec,rc,dec in db.execute("SELECT id,name,target,unit,COALESCE(phase,0),COALESCE(kind,'chunked'),COALESCE(record_type,'none'),COALESCE(repeat_count,3),COALESCE(decimals,0) FROM activities WHERE active=1 ORDER BY sort,id").fetchall():
        notes = db.execute("SELECT COUNT(*) FROM comments WHERE activity_id=? AND hidden=0",(aid,)).fetchone()[0]
        if kind == "repeat":
            L = reps.get(aid, []); n = min(len(L), rc)
            out.append({"id":aid,"name":name,"target":rc,"unit":unit,"phase":phase,"kind":"repeat","record":"none","total":n,"pct":n*100//rc,"done":n>=rc,"repeats":L[:rc],"chosen":aid in chosen,"notes":notes})
            continue
        if kind == "once":
            r = res.get(aid)
            out.append({"id":aid,"name":name,"target":1,"unit":unit,"phase":phase,"kind":"once","record":rec,"total":1 if r else 0,"pct":100 if r else 0,"done":bool(r),
                        "result":({"seconds":r[0],"weight":r[1],"unit":r[2]} if r else None),"chosen":aid in chosen,"notes":notes})
            continue
        tot = db.execute("SELECT COALESCE(SUM(amount),0) FROM progress WHERE user_id=? AND activity_id=?", (uid,aid)).fetchone()[0]
        tot = min(tot, target)
        out.append({"id":aid,"name":name,"target":target,"unit":unit,"phase":phase,"kind":"chunked","record":"none","total":tot,"pct":int(tot*100//target) if target else 0,"done":tot>=target,"chosen":aid in chosen,"notes":notes,"dec":dec})
    return out

def me_payload(u):
    s = stats(u[0])
    acts = activities_for(u[0])
    got = {r[0] for r in db.execute("SELECT key FROM badges WHERE user_id=?",(u[0],))}
    return {"name":u[1], "today":today().isoformat(), "start":start_end()[0].isoformat(), "end":start_end()[1].isoformat(), "beta":is_beta(),
            "total":TOTAL, "badgeList":[dict(badge_pub(b), got=b["key"] in got, hint="" if b["key"] in got else badge_hint(b, acts)) for b in BADGES], "activities":acts, "activitiesDone":sum(1 for a in acts if a["done"]),"picksSaved":bool(db.execute("SELECT 1 FROM user_picks WHERE user_id=?", (u[0],)).fetchone()), **s}

def fdate(t): return datetime.fromtimestamp(t, TZ).date().isoformat() if t else ""
def member_rows():
    acts = db.execute("SELECT COUNT(*) FROM activities WHERE active=1").fetchone()[0]
    out = []
    for uid,name,created in db.execute("SELECT id,name,created FROM users").fetchall():
        st = stats(uid)
        last = db.execute("SELECT MAX(m) FROM (SELECT MAX(created) m FROM checkins WHERE user_id=?1 UNION ALL SELECT MAX(created) FROM progress WHERE user_id=?1 UNION ALL SELECT MAX(created) FROM results WHERE user_id=?1 UNION ALL SELECT MAX(created) FROM repeats WHERE user_id=?1 UNION ALL SELECT MAX(created) FROM comments WHERE user_id=?1)", (uid,)).fetchone()[0] or created or 0
        out.append({"id":uid,"name":name,"joined":fdate(created),"joined_ts":created or 0,"days":st["done"],"streak":st["current"],
            "completed":(cd:=sum(1 for a in activities_for(uid) if a["done"])),"raffle":(2 if cd>=20 else 1 if cd>=10 else 0),"finished30":cd>=30,"last_active":fdate(last),"last_ts":last,
            "notes":db.execute("SELECT COUNT(*) FROM comments WHERE user_id=?", (uid,)).fetchone()[0]})
    out.sort(key=lambda r:-r["last_ts"])
    return {"total":len(out),"active7":sum(1 for r in out if r["last_ts"] >= time.time()-7*86400),"activities":acts,"members":out}
def member_detail(uid):
    u = db.execute("SELECT id,name,created FROM users WHERE id=?", (uid,)).fetchone()
    if not u: return None
    nm = dict(db.execute("SELECT id,name FROM activities").fetchall())
    tg = {r[0]:r[1] for r in db.execute("SELECT id,target FROM activities")}
    d = {"id":u[0],"name":u[1],"joined":fdate(u[2])}
    d["checkins"] = [{"day":r[0],"note":r[1] or ""} for r in db.execute("SELECT day,note FROM checkins WHERE user_id=? ORDER BY day DESC", (uid,))]
    d["picks"] = [nm.get(r[0],"?") for r in db.execute("SELECT activity_id FROM user_activities WHERE user_id=?", (uid,))]
    prog = {}
    for aid,amt,c in db.execute("SELECT activity_id,amount,created FROM progress WHERE user_id=? ORDER BY id", (uid,)):
        p = prog.setdefault(aid, {"activity":nm.get(aid,"?"),"target":tg.get(aid),"total":0,"entries":[]}); p["total"] += amt; p["entries"].append({"amount":amt,"date":fdate(c)})
    d["progress"] = list(prog.values())
    d["results"] = [{"activity":nm.get(r[0],"?"),"seconds":r[1],"weight":r[2],"unit":r[3],"date":fdate(r[4])} for r in db.execute("SELECT activity_id,seconds,weight,wunit,created FROM results WHERE user_id=?", (uid,))]
    d["repeats"] = [{"activity":nm.get(r[0],"?"),"day":r[1],"note":r[2] or "","date":fdate(r[3])} for r in db.execute("SELECT activity_id,day,note,created FROM repeats WHERE user_id=? ORDER BY id", (uid,))]
    d["comments"] = [{"activity":nm.get(r[0],"?"),"body":r[1],"date":fdate(r[2]),"hidden":bool(r[3])} for r in db.execute("SELECT activity_id,body,created,hidden FROM comments WHERE user_id=? ORDER BY id DESC", (uid,))]
    return d
def csv_text(rows):
    o = io.StringIO(); w = csv.writer(o)
    for r in rows: w.writerow(["'"+c if isinstance(c,str) and c and c[0] in "=+-@" else c for c in r])
    return o.getvalue().encode()

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
    def _route(self, fn):
        p = self.path.split("?")[0]
        if p == "/beta" or p.startswith("/beta/"):
            _TL.conn = BETA; p = p[5:] or "/"
            if p.startswith("/api/federation/"): return self.send(404, {"error":"nf"})
        else: _TL.conn = None
        try: return fn(p)
        finally: _TL.conn = None
    def do_GET(self): return self._route(self._get)
    def do_POST(self): return self._route(self._post)
    def _get(self, p):
        if p in ("/","/index.html"):
            h = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"index.html"),"rb").read()
            if is_beta(): h = h.replace(b"<head>", b"<head><script>window.BETA=1</script>", 1)
            return self.send(200, h, "text/html; charset=utf-8")
        if p == "/admin":
            return self.send(200, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"admin.html"),"rb").read(), "text/html; charset=utf-8")
        if p == "/api/me":
            u = user_from(self)
            return self.send(200, me_payload(u)) if u else self.send(401, {"error":"auth"})
        if p == "/api/wins":
            rows = [r for r in db.execute("SELECT u.name,w.key,w.created FROM badges w JOIN users u ON u.id=w.user_id ORDER BY w.created DESC LIMIT 40").fetchall() if r[1] in BADGE][:30]
            cnt = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            return self.send(200, {"members":cnt, "wins":[{"name":r[0],"key":r[1],"label":BADGE[r[1]]["label"],"icon":BADGE[r[1]]["icon"],"at":r[2]} for r in rows]})
        if p == "/api/leaderboard":
            # Momentum Board: every member, alphabetical, one snowflake per 5 completed activities
            u = user_from(self); rows = []
            for uid, name in db.execute("SELECT id,name FROM users").fetchall():
                n = sum(1 for a in activities_for(uid) if a["done"])
                rows.append({"name":name, "acts":n, "flakes":min(6, n // 5), "me":bool(u and uid == u[0])})
            rows.sort(key=lambda r: r["name"].casefold())
            return self.send(200, {"total":len(rows), "members":rows})
        self.send(404, {"error":"nf"})
    def _post(self, p):
        b = self.body()
        # shared-login federation + login/register take the lock themselves (they may call the peer app, never while holding it)
        if p.startswith("/api/federation/"): return FED.handle(self, p, b)
        if p == "/api/login": return self.login(b)
        if p == "/api/register": return self.register(b)
        with LOCK:
            if p == "/api/logout":
                m = re.search(r"(?:^|;)\s*%s=([A-Za-z0-9_\-]+)" % CK(), self.headers.get("Cookie",""))
                if m: db.execute("DELETE FROM sessions WHERE token_hash=?", (th(m.group(1)),))
                return self.send(200, {"ok":1}, cookie="%s=; Path=/; Max-Age=0" % CK())
            if p.startswith("/api/admin/"): return self.admin(p, b)
            u = user_from(self)
            if not u: return self.send(401, {"error":"auth"})
            if p == "/api/comments/list": return self.comments_list(u, b)
            if p == "/api/comments": return self.comment_post(u, b)
            if p == "/api/comments/delete": return self.comment_delete(u, b)
            if p == "/api/comments/react": return self.comment_react(u, b)
            if p == "/api/repeat": return self.repeat_done(u, b)
            if p == "/api/repeat/undo": return self.undo_repeat(u, b)
            if p == "/api/result": return self.record_result(u, b)
            if p == "/api/result/undo": return self.undo_result(u, b)
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
        with LOCK:
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (th(tok), uid, now))
            nm = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()[0]
            pl = me_payload((uid,nm))
        self.send(200, {"new":new, **pl}, cookie=f"{CK()}={tok}; Path=/; Max-Age=7776000; HttpOnly; SameSite=Lax; Secure")
    def login(self, b):
        # Log in only: never creates an account on a failed login. The local check comes first; then the peer app is asked.
        p = self._parse(b)
        if not p: return
        name, pin = p
        r = self.beta_login(name, pin) if is_beta() else FED.login(name, pin)
        if r[0] == "ok": return self._session(r[1], r[2])
        if r[0] == "locked": return self.send(429, {"error":"Too many tries. Please wait 5 minutes, then try again. Forgot your PIN? Email Memberservice@gymnazo.com or ask any coach."})
        msg = "That name or PIN doesn't match. Please try again."
        if r[1]: msg += " (If you signed up on our other Gymnazo app, we can't check it right now. Please try again in a few minutes.)"
        self.send(401, {"error":msg})
    def register(self, b):
        p = self._parse(b)
        if not p: return
        name, pin = p; key = name.casefold()
        taken = {"error":"That name is already taken. If that's you, tap \"Log in\" and use the same PIN you picked before (it works on both Gymnazo apps). If not, please pick a slightly different name (like adding your last initial)."}
        with LOCK:
            if db.execute("SELECT 1 FROM users WHERE name_key=?", (key,)).fetchone(): return self.send(409, taken)
        c = ("ok",) if is_beta() else FED.register_check(name)
        if c[0] == "taken": return self.send(409, taken)
        if c[0] == "peer_down": return self.send(503, {"error":"We can't finish creating your account right now. Please try again in a few minutes."})
        salt = secrets.token_hex(16); now = time.time()
        with LOCK:
            if db.execute("SELECT 1 FROM users WHERE name_key=?", (key,)).fetchone(): return self.send(409, taken)
            cur = db.execute("INSERT INTO users(name,name_key,salt,pin_hash,created,pin_changed) VALUES(?,?,?,?,?,?)", (name,key,salt,hp(pin,salt),now,now))
            uid = cur.lastrowid
        self._session(uid, True)
    def beta_login(self, name, pin):
        # standalone beta login: local beta DB only, never asks the Metrics app
        with LOCK:
            r = db.execute("SELECT id,salt,pin_hash,fails,locked_until FROM users WHERE name_key=?", (name.casefold(),)).fetchone()
            if not r: return ("bad", False)
            if r[4] > time.time(): return ("locked",)
            if hmac.compare_digest(hp(pin, r[1]), r[2]):
                db.execute("UPDATE users SET fails=0 WHERE id=?", (r[0],)); return ("ok", r[0], False)
            f = r[3] + 1
            if f >= 8: db.execute("UPDATE users SET fails=0,locked_until=? WHERE id=?", (time.time()+300, r[0]))
            else: db.execute("UPDATE users SET fails=? WHERE id=?", (f, r[0]))
            return ("bad", False)
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
        try: aid = int(b.get("activity_id")); raw = float(b.get("amount"))
        except Exception: return self.send(400, {"error":"Please type a number."})
        a = db.execute("SELECT target,COALESCE(kind,'chunked'),COALESCE(decimals,0) FROM activities WHERE id=? AND active=1", (aid,)).fetchone()
        if not a: return self.send(404, {"error":"That activity isn't available."})
        if raw != raw or abs(raw) > 1e7: return self.send(400, {"error":"Please type a number."})
        amt = int(round(raw * 10 ** a[2]))
        if a[1] != "chunked": return self.send(400, {"error":"This one isn't added up in pieces. Please use its own button."})
        if amt < 1 or amt > 1000000: return self.send(400, {"error":"Please type a number bigger than 0."})
        tot = db.execute("SELECT COALESCE(SUM(amount),0) FROM progress WHERE user_id=? AND activity_id=?", (u[0],aid)).fetchone()[0]
        room = a[0] - tot
        if room <= 0: return self.send(400, {"error":"You already finished this one. Amazing!"})
        amt = min(amt, room)
        db.execute("INSERT INTO progress(user_id,activity_id,amount,created) VALUES(?,?,?,?)", (u[0],aid,amt,time.time()))
        self.send(200, {"added":amt, "newBadges":sync_badges(u[0]), **me_payload(u)})
    def record_result(self, u, b):
        try: aid = int(b.get("activity_id"))
        except Exception: return self.send(400, {"error":"bad activity"})
        a = db.execute("SELECT COALESCE(kind,'chunked'),COALESCE(record_type,'none') FROM activities WHERE id=? AND active=1", (aid,)).fetchone()
        if not a: return self.send(404, {"error":"That activity isn't available."})
        if a[0] != "once": return self.send(400, {"error":"Use the add buttons for this one."})
        rec = a[1]; secs = None; wt = None; wu = None
        def num(v, default=0):
            try:
                if v in (None, ""): return default
                x = float(v)
                return x if x == x and abs(x) != float("inf") else None
            except Exception: return None
        if rec in ("time","both"):
            m = num(b.get("minutes")); sc = num(b.get("seconds"))
            if m is None or sc is None or m < 0 or sc < 0 or sc > 59 or m > 360:
                return self.send(400, {"error":"Please enter minutes (0 to 360) and seconds (0 to 59)."})
            total = int(m)*60 + int(sc)
            if total == 0 and rec == "time": return self.send(400, {"error":"Please tell us how long it took."})
            if total > 21600: return self.send(400, {"error":"That time looks too long. Please check it."})
            secs = total or None
        if rec in ("weight","both"):
            wt = num(b.get("weight"), None); wu = str(b.get("unit","lb")).lower()
            if wu not in ("lb","kg"): return self.send(400, {"error":"Please choose lb or kg."})
            if wt is None or wt <= 0 or wt > (1500 if wu == "lb" else 700):
                return self.send(400, {"error":"Please enter the weight you used (a number bigger than 0)."})
            wt = round(wt, 1)
        first = not db.execute("SELECT 1 FROM results WHERE user_id=? AND activity_id=?", (u[0],aid)).fetchone()
        db.execute("INSERT INTO results(user_id,activity_id,seconds,weight,wunit,created) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,activity_id) DO UPDATE SET seconds=excluded.seconds,weight=excluded.weight,wunit=excluded.wunit", (u[0],aid,secs,wt,wu,time.time()))
        self.send(200, {"first":first, "newBadges":sync_badges(u[0]), **me_payload(u)})
    def repeat_done(self, u, b):
        try: aid = int(b.get("activity_id"))
        except Exception: return self.send(400, {"error":"bad activity"})
        a = db.execute("SELECT COALESCE(kind,'chunked'),COALESCE(repeat_count,3) FROM activities WHERE id=? AND active=1", (aid,)).fetchone()
        if not a or a[0] != "repeat": return self.send(404, {"error":"That activity isn't available."})
        n, last = db.execute("SELECT COUNT(*),MAX(created) FROM repeats WHERE user_id=? AND activity_id=?", (u[0],aid)).fetchone()
        if n >= a[1]: return self.send(400, {"error":"You've done all %d. Amazing!" % a[1]})
        now = time.time()
        if last and now - last < 5: return self.send(429, {"error":"Got it! Please wait a few seconds before tapping again."})
        note = re.sub(r"<[^>]*>", "", str(b.get("note",""))); note = re.sub(r"\s+"," ",note).strip()[:100]
        db.execute("INSERT INTO repeats(user_id,activity_id,day,note,created) VALUES(?,?,?,?,?)", (u[0],aid,today().isoformat(),note,now))
        self.send(200, {"count":n+1,"complete":n+1>=a[1], "newBadges":sync_badges(u[0]), **me_payload(u)})
    def undo_repeat(self, u, b):
        try: aid = int(b.get("activity_id"))
        except Exception: return self.send(400, {"error":"bad activity"})
        r = db.execute("SELECT id FROM repeats WHERE user_id=? AND activity_id=? ORDER BY id DESC LIMIT 1", (u[0],aid)).fetchone()
        if not r: return self.send(400, {"error":"Nothing to undo."})
        db.execute("DELETE FROM repeats WHERE id=?", (r[0],)); sync_badges(u[0])
        self.send(200, me_payload(u))
    def undo_result(self, u, b):
        try: aid = int(b.get("activity_id"))
        except Exception: return self.send(400, {"error":"bad activity"})
        db.execute("DELETE FROM results WHERE user_id=? AND activity_id=?", (u[0],aid)); sync_badges(u[0])
        self.send(200, me_payload(u))
    def undo_progress(self, u, b):
        try: aid = int(b.get("activity_id"))
        except Exception: return self.send(400, {"error":"bad activity"})
        r = db.execute("SELECT id FROM progress WHERE user_id=? AND activity_id=? ORDER BY id DESC LIMIT 1", (u[0],aid)).fetchone()
        if not r: return self.send(400, {"error":"Nothing to undo."})
        db.execute("DELETE FROM progress WHERE id=?", (r[0],)); sync_badges(u[0])
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
            rows = db.execute("SELECT id,name,target,unit,sort,active,COALESCE(phase,0),COALESCE(kind,'chunked'),COALESCE(record_type,'none'),COALESCE(repeat_count,3) FROM activities ORDER BY sort,id").fetchall()
            return {"activities":[{"id":r[0],"name":r[1],"target":r[2],"unit":r[3],"sort":r[4],"active":bool(r[5]),"phase":r[6],"kind":r[7],"record":r[8],"repeat_count":r[9],
                    "finished":(db.execute("SELECT COUNT(*) FROM results WHERE activity_id=?", (r[0],)).fetchone()[0] if r[7]=="once" else db.execute("SELECT COUNT(*) FROM (SELECT user_id FROM repeats WHERE activity_id=? GROUP BY user_id HAVING COUNT(*)>=?)", (r[0],r[9])).fetchone()[0] if r[7]=="repeat" else db.execute("SELECT COUNT(*) FROM (SELECT user_id FROM progress WHERE activity_id=? GROUP BY user_id HAVING SUM(amount)>=?)", (r[0],r[2])).fetchone()[0])} for r in rows]}
        if p == "/api/admin/list": return self.send(200, lst())
        if p == "/api/admin/save":
            name = str(b.get("name","")).strip()[:60]; unit = str(b.get("unit","")).strip()[:30]
            try: target = int(b.get("target"))
            except Exception: target = 0
            kind = b.get("kind","chunked"); rec = b.get("record","none")
            if kind not in KINDS or rec not in RECS: return self.send(400, {"error":"Please choose a valid Kind and Record."})
            try: rcount = int(b.get("repeat_count", 3))
            except Exception: rcount = 3
            if not 1 <= rcount <= 10: rcount = 3
            if kind == "once": target = 1; unit = unit or "effort"
            if kind == "repeat": target = rcount; unit = unit or "times"; rec = "none"
            if not name or not unit or target < 1 or target > 1000000: return self.send(400, {"error":"Please give a name, a unit, and a target between 1 and 1,000,000."})
            active = 1 if b.get("active", True) else 0
            try: phase = int(b.get("phase", 0))
            except Exception: phase = 0
            if phase not in (0,1,2,3): phase = 0
            if b.get("id"):
                db.execute("UPDATE activities SET name=?,target=?,unit=?,active=?,phase=?,kind=?,record_type=?,repeat_count=? WHERE id=?", (name,target,unit,active,phase,kind,rec,rcount,int(b["id"])))
            else:
                mx = db.execute("SELECT COALESCE(MAX(sort),-1)+1 FROM activities").fetchone()[0]
                db.execute("INSERT INTO activities(name,target,unit,sort,active,phase,kind,record_type,repeat_count) VALUES(?,?,?,?,?,?,?,?,?)", (name,target,unit,mx,active,phase,kind,rec,rcount))
            return self.send(200, lst())
        if p == "/api/admin/bulk":
            text = str(b.get("text",""))[:100000]; dry = bool(b.get("dry"))
            seen = {r[0].casefold() for r in db.execute("SELECT name FROM activities WHERE active=1")}
            added, skipped, bad = [], [], []
            for n, line in enumerate(text.splitlines(), 1):
                if not line.strip(): continue
                parts = [x.strip() for x in line.split("|")]
                name = parts[0][:60]
                kind = parts[4].casefold() if len(parts) > 4 and parts[4] else "chunked"
                rec = parts[5].casefold() if len(parts) > 5 and parts[5] else "none"
                try: target = int(parts[1].replace(",","")) if len(parts) > 1 else 0
                except Exception: target = 0
                if kind == "once": target = 1
                if kind == "repeat": target = 3; rec = "none"
                ph = 0
                if len(parts) > 3 and parts[3]:
                    ph = PHASES.get(parts[3].casefold(), -1)
                if len(parts) > 6 or ph < 0 or kind not in KINDS or rec not in RECS or not name or target < 1 or target > 1000000:
                    bad.append({"line":n,"text":line.strip()[:80]}); continue
                unit = (parts[2] if len(parts) > 2 and parts[2] else ("effort" if kind == "once" else "times"))[:30]
                if name.casefold() in seen: skipped.append({"line":n,"name":name}); continue
                seen.add(name.casefold()); added.append({"line":n,"name":name,"target":target,"unit":unit,"phase":ph,"kind":kind,"record":rec})
            if not dry:
                for a in added:
                    mx = db.execute("SELECT COALESCE(MAX(sort),-1)+1 FROM activities").fetchone()[0]
                    db.execute("INSERT INTO activities(name,target,unit,sort,active,phase,kind,record_type,repeat_count) VALUES(?,?,?,?,1,?,?,?,3)", (a["name"],a["target"],a["unit"],mx,a["phase"],a["kind"],a["record"]))
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
        if p == "/api/admin/delete":
            try: aid = int(b.get("id"))
            except Exception: return self.send(400, {"error":"bad request"})
            used = db.execute("SELECT (SELECT COUNT(*) FROM progress WHERE activity_id=?)+(SELECT COUNT(*) FROM results WHERE activity_id=?)+(SELECT COUNT(*) FROM comments WHERE activity_id=?)+(SELECT COUNT(*) FROM repeats WHERE activity_id=?)", (aid,aid,aid,aid)).fetchone()[0]
            if used: return self.send(409, {"error":"Members have already used this one, so it can't be deleted. Hide it instead.","used":used})
            db.execute("DELETE FROM user_activities WHERE activity_id=?", (aid,)); db.execute("DELETE FROM activities WHERE id=?", (aid,))
            return self.send(200, {"deleted":True, **lst()})
        if p == "/api/admin/reorder":
            ids = b.get("ids", [])
            if isinstance(ids, list):
                for i,x in enumerate(ids):
                    try: db.execute("UPDATE activities SET sort=? WHERE id=?", (i,int(x)))
                    except Exception: pass
            return self.send(200, lst())
        if p == "/api/admin/beta/wipe":
            if str(b.get("confirm","")).strip().upper() != "WIPE BETA": return self.send(400, {"error":"Type WIPE BETA to confirm."})
            beta_wipe(); return self.send(200, {"ok":1, "wiped":"beta"})
        if p == "/api/admin/beta/stats":
            return self.send(200, {"users":BETA.execute("SELECT COUNT(*) FROM users").fetchone()[0], "checkins":BETA.execute("SELECT COUNT(*) FROM checkins").fetchone()[0], "progress":BETA.execute("SELECT COUNT(*) FROM progress").fetchone()[0]})
        if p.startswith("/api/admin/edit/"): return self.admin_edit(p[16:], b)
        if p == "/api/admin/members": return self.send(200, member_rows())
        if p == "/api/admin/members/detail":
            try: d = member_detail(int(b.get("id")))
            except Exception: d = None
            return self.send(200, d) if d else self.send(404, {"error":"No such member."})
        if p == "/api/admin/members/csv":
            m = member_rows()
            rows = [["name","joined","days_checked_in","current_streak","challenges_completed","challenges_total","raffle_entries","finished_all_30","last_active","notes_posted"]]
            rows += [[r["name"],r["joined"],r["days"],r["streak"],r["completed"],m["activities"],r["raffle"],"yes" if r["finished30"] else "no",r["last_active"],r["notes"]] for r in m["members"]]
            return self.send(200, csv_text(rows), "text/csv; charset=utf-8")
        if p == "/api/admin/members/inputs-csv":
            rows = [["member","type","activity","date","value","note"]]
            for r in member_rows()["members"]:
                d = member_detail(r["id"]); n = d["name"]
                for c in d["checkins"]: rows.append([n,"check-in","",c["day"],"",c["note"]])
                for a in d["picks"]: rows.append([n,"picked","","","",a])
                for a in d["progress"]:
                    for e in a["entries"]: rows.append([n,"progress",a["activity"],e["date"],e["amount"],"total %s of %s"%(a["total"],a["target"])])
                for x in d["results"]: rows.append([n,"result",x["activity"],x["date"],"; ".join(filter(None,[("%dm%02ds"%divmod(x["seconds"],60)) if x["seconds"] else "", ("%s %s"%(x["weight"],x["unit"])) if x["weight"] else ""])),""])
                for x in d["repeats"]: rows.append([n,"repeat",x["activity"],x["day"],"",x["note"]])
                for x in d["comments"]: rows.append([n,"note",x["activity"],x["date"],"hidden" if x["hidden"] else "",x["body"]])
            return self.send(200, csv_text(rows), "text/csv; charset=utf-8")
        if p == "/api/admin/members/reset-pin":
            pin = str(b.get("pin",""))
            if not re.fullmatch(r"\d{4}", pin): return self.send(400, {"error":"The new PIN must be exactly 4 digits."})
            try: uid = int(b.get("id"))
            except Exception: return self.send(400, {"error":"bad request"})
            if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone(): return self.send(404, {"error":"No such member."})
            salt = secrets.token_hex(16); ph = hp(pin,salt); ts = time.time()
            db.execute("UPDATE users SET salt=?,pin_hash=?,pin_changed=?,fails=0,locked_until=0 WHERE id=?", (salt,ph,ts,uid))
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            # carry the reset to the peer app (existing accounts only); retried in the background if it is unreachable
            FED.queue_pin(db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()[0], salt, ph, ts, have_lock=True)
            return self.send(200, {"ok":1, "federated": federation.enabled()})
        if p == "/api/admin/members/delete":
            try: uid = int(b.get("id"))
            except Exception: return self.send(400, {"error":"bad request"})
            r = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
            if not r: return self.send(404, {"error":"No such member."})
            if str(b.get("confirm","")).strip().casefold() != r[0].casefold(): return self.send(400, {"error":"Type the member's name exactly to confirm."})
            db.execute("DELETE FROM reactions WHERE user_id=? OR comment_id IN (SELECT id FROM comments WHERE user_id=?)", (uid,uid))
            db.execute("CREATE TABLE IF NOT EXISTS admin_audit(ts REAL, user_id INTEGER, action TEXT, detail TEXT)")
            for t in ("admin_audit","sessions","checkins","wins","badges","progress","user_activities","user_picks","comments","results","repeats"):
                db.execute("DELETE FROM %s WHERE user_id=?" % t, (uid,))
            db.execute("DELETE FROM users WHERE id=?", (uid,))
            return self.send(200, {"deleted":True, **member_rows()})
        self.send(404, {"error":"nf"})
    def admin_edit(self, op, b):
        """Studio edits of one member's inputs. Every change re-checks badges and is written to the audit log."""
        try: uid = int(b.get("id"))
        except Exception: return self.send(400, {"error":"bad request"})
        if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone(): return self.send(404, {"error":"No such member."})
        db.execute("CREATE TABLE IF NOT EXISTS admin_audit(ts REAL, user_id INTEGER, action TEXT, detail TEXT)")
        now = time.time()
        def act():
            try: aid = int(b.get("activity_id"))
            except Exception: return None
            return db.execute("SELECT id,name,target,COALESCE(kind,'chunked'),COALESCE(decimals,0),COALESCE(repeat_count,3),COALESCE(record_type,'none') FROM activities WHERE id=?", (aid,)).fetchone()
        def num(v):
            try:
                x = float(v); return x if x == x and abs(x) < 1e7 else None
            except Exception: return None
        def day(v):
            try: return date.fromisoformat(str(v)).isoformat()
            except Exception: return None
        a = act(); detail = ""
        if op == "state": return self.send(200, self._edit_state(uid))
        if op in ("progress/set", "progress/add"):
            if not a or a[3] != "chunked": return self.send(400, {"error":"That activity isn't added up in amounts."})
            x = num(b.get("amount"))
            if x is None or x < 0: return self.send(400, {"error":"Please type a number (0 or more)."})
            amt = min(int(round(x * 10 ** a[4])), a[2] if op == "progress/set" else 10**9)
            if op == "progress/set":
                db.execute("DELETE FROM progress WHERE user_id=? AND activity_id=?", (uid, a[0]))
                if amt > 0: db.execute("INSERT INTO progress(user_id,activity_id,amount,created) VALUES(?,?,?,?)", (uid, a[0], amt, now))
            else:
                if amt < 1: return self.send(400, {"error":"Please type a number bigger than 0."})
                db.execute("INSERT INTO progress(user_id,activity_id,amount,created) VALUES(?,?,?,?)", (uid, a[0], amt, now))
            detail = "%s %s %s" % (op, a[1], x)
        elif op in ("progress/update", "progress/delete"):
            try: eid = int(b.get("entry_id"))
            except Exception: return self.send(400, {"error":"bad request"})
            r = db.execute("SELECT p.activity_id,a.decimals FROM progress p JOIN activities a ON a.id=p.activity_id WHERE p.id=? AND p.user_id=?", (eid, uid)).fetchone()
            if not r: return self.send(404, {"error":"That entry is gone."})
            if op == "progress/delete": db.execute("DELETE FROM progress WHERE id=?", (eid,))
            else:
                x = num(b.get("amount"))
                if x is None or x <= 0: return self.send(400, {"error":"Please type a number bigger than 0."})
                db.execute("UPDATE progress SET amount=? WHERE id=?", (int(round(x * 10 ** (r[1] or 0))), eid))
            detail = "%s entry %d" % (op, eid)
        elif op in ("result/set", "result/delete"):
            if not a or a[3] != "once": return self.send(400, {"error":"That activity has no recorded result."})
            if op == "result/delete": db.execute("DELETE FROM results WHERE user_id=? AND activity_id=?", (uid, a[0]))
            else:
                m, sc, wt = num(b.get("minutes") or 0), num(b.get("seconds") or 0), b.get("weight")
                if m is None or sc is None or m < 0 or sc < 0 or sc > 59: return self.send(400, {"error":"Minutes and seconds (0-59) please."})
                secs = int(m) * 60 + int(sc) or None
                wt = num(wt) if wt not in (None, "") else None
                wu = str(b.get("unit") or "lb").lower()
                if wu not in ("lb","kg"): wu = "lb"
                db.execute("INSERT INTO results(user_id,activity_id,seconds,weight,wunit,created) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,activity_id) DO UPDATE SET seconds=excluded.seconds,weight=excluded.weight,wunit=excluded.wunit", (uid, a[0], secs, round(wt,1) if wt else None, wu if wt else None, now))
            detail = "%s %s" % (op, a[1])
        elif op == "repeat/add":
            if not a or a[3] != "repeat": return self.send(400, {"error":"That activity isn't a check-off one."})
            if db.execute("SELECT COUNT(*) FROM repeats WHERE user_id=? AND activity_id=?", (uid, a[0])).fetchone()[0] >= a[5]: return self.send(400, {"error":"All %d are already done." % a[5]})
            d = day(b.get("day")) or today().isoformat()
            db.execute("INSERT INTO repeats(user_id,activity_id,day,note,created) VALUES(?,?,?,?,?)", (uid, a[0], d, "edited by studio", now)); detail = "repeat/add %s %s" % (a[1], d)
        elif op == "repeat/delete":
            try: eid = int(b.get("entry_id"))
            except Exception: return self.send(400, {"error":"bad request"})
            db.execute("DELETE FROM repeats WHERE id=? AND user_id=?", (eid, uid)); detail = "repeat/delete %d" % eid
        elif op == "complete":
            if not a: return self.send(404, {"error":"No such activity."})
            done = bool(b.get("done"))
            if a[3] == "chunked":
                db.execute("DELETE FROM progress WHERE user_id=? AND activity_id=?", (uid, a[0]))
                if done: db.execute("INSERT INTO progress(user_id,activity_id,amount,created) VALUES(?,?,?,?)", (uid, a[0], a[2], now))
            elif a[3] == "once":
                if done: db.execute("INSERT OR IGNORE INTO results(user_id,activity_id,seconds,weight,wunit,created) VALUES(?,?,NULL,NULL,NULL,?)", (uid, a[0], now))
                else: db.execute("DELETE FROM results WHERE user_id=? AND activity_id=?", (uid, a[0]))
            else:
                if done:
                    n = db.execute("SELECT COUNT(*) FROM repeats WHERE user_id=? AND activity_id=?", (uid, a[0])).fetchone()[0]
                    for _ in range(max(0, a[5] - n)): db.execute("INSERT INTO repeats(user_id,activity_id,day,note,created) VALUES(?,?,?,?,?)", (uid, a[0], today().isoformat(), "edited by studio", now))
                else: db.execute("DELETE FROM repeats WHERE user_id=? AND activity_id=?", (uid, a[0]))
            if done: db.execute("INSERT OR IGNORE INTO user_activities VALUES(?,?,?)", (uid, a[0], now))
            detail = "%s %s" % ("complete" if done else "incomplete", a[1])
        elif op in ("checkin/add", "checkin/delete"):
            d = day(b.get("day"))
            if not d: return self.send(400, {"error":"Please pick a date."})
            if op == "checkin/add": db.execute("INSERT OR IGNORE INTO checkins VALUES(?,?,?,?)", (uid, d, "edited by studio", now))
            else: db.execute("DELETE FROM checkins WHERE user_id=? AND day=?", (uid, d))
            detail = "%s %s" % (op, d)
        else: return self.send(404, {"error":"nf"})
        sync_badges(uid)
        db.execute("INSERT INTO admin_audit VALUES(?,?,?,?)", (now, uid, "edited by studio", detail))
        self.send(200, self._edit_state(uid))
    def _edit_state(self, uid):
        nm = db.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()[0]
        acts = activities_for(uid)
        ent = {}
        for i, aid, amt, c in db.execute("SELECT id,activity_id,amount,created FROM progress WHERE user_id=? ORDER BY id", (uid,)): ent.setdefault(aid, []).append({"id":i,"amount":amt,"date":fdate(c)})
        rep = {}
        for i, aid, d, n in db.execute("SELECT id,activity_id,day,note FROM repeats WHERE user_id=? ORDER BY id", (uid,)): rep.setdefault(aid, []).append({"id":i,"day":d,"note":n or ""})
        for a in acts:
            if a["kind"] == "chunked": a["entries"] = ent.get(a["id"], [])
            if a["kind"] == "repeat": a["repeats"] = rep.get(a["id"], [])
        done = sum(1 for a in acts if a["done"])
        db.execute("CREATE TABLE IF NOT EXISTS admin_audit(ts REAL, user_id INTEGER, action TEXT, detail TEXT)")
        return {"id":uid, "name":nm, "activities":acts, "completed":done, "pct":round(min(done,30)/30*100), "raffle":2 if done>=20 else 1 if done>=10 else 0,
                "checkins":[r[0] for r in db.execute("SELECT day FROM checkins WHERE user_id=? ORDER BY day", (uid,))],
                "badges":[BADGE[r[0]]["label"] for r in db.execute("SELECT key FROM badges WHERE user_id=? ORDER BY created", (uid,)) if r[0] in BADGE],
                "audit":[{"at":fdate(r[0]),"detail":r[1]} for r in db.execute("SELECT ts,detail FROM admin_audit WHERE user_id=? ORDER BY ts DESC LIMIT 15", (uid,))]}
    def checkin(self, u, b):
        try: d = date.fromisoformat(str(b.get("date")))
        except Exception: return self.send(400, {"error":"bad date"})
        s0, e0 = start_end()
        if d < s0 or d > e0 or d > today(): return self.send(400, {"error":"That day isn't open for check-in."})
        new = []
        if b.get("done", True):
            note = str(b.get("note",""))[:200]
            db.execute("INSERT INTO checkins VALUES(?,?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET note=excluded.note", (u[0], d.isoformat(), note, time.time()))
            new = sync_badges(u[0])
        else:
            db.execute("DELETE FROM checkins WHERE user_id=? AND day=?", (u[0], d.isoformat())); sync_badges(u[0])
        self.send(200, {"newBadges":new, **me_payload(u)})

class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True
if __name__ == "__main__":
    S((os.environ.get("HOST", "0.0.0.0"), PORT), H).serve_forever()
