"""Shared-login federation tests: runs the Winter Solstice app and the Metrics app side by side, pointed at each other.
Usage: python3 tests/test_federation.py   (set SOLSTICE_DIR / METRICS_DIR if the repos are not siblings)"""
import subprocess, sys, os, time, json, http.client, tempfile, sqlite3, re
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def find(envname, names):
    if os.environ.get(envname): return os.environ[envname]
    for n in names:
        for base in (os.path.dirname(HERE), HERE):
            p = os.path.join(base, n)
            if os.path.exists(os.path.join(p, "server.py")) and p != HERE: return p
    raise SystemExit("Cannot find the other repo; set " + envname)
if os.path.exists(os.path.join(HERE, "exercises.json")):
    MET, SOL = HERE, find("SOLSTICE_DIR", ["solstice-fed", "solstice-challenge", "solstice-challenge-ui"])
else:
    SOL, MET = HERE, find("METRICS_DIR", ["gymnazo-metrics"])
tmp = tempfile.mkdtemp(); SP, MP = 18101, 18102; SDB, MDB = os.path.join(tmp, "s.db"), os.path.join(tmp, "m.db")
SECRET = "test-secret-" + "x" * 20
base = dict(os.environ, FEDERATION_SECRET=SECRET, FED_RETRY_SECS="1.5", FED_TIMEOUT="1.5", HOST="127.0.0.1", MX_TEST_TODAY="2026-10-10", SOL_TEST_TODAY="2026-10-25")
def start(which):
    if which == "sol": e = dict(base, PORT=str(SP), DB_PATH=SDB, ADMIN_PASSWORD="sa", PEER_URL=f"http://127.0.0.1:{MP}"); d = SOL
    else: e = dict(base, PORT=str(MP), DB_PATH=MDB, ADMIN_PASSWORD="ma", PEER_URL=f"http://127.0.0.1:{SP}"); d = MET
    p = subprocess.Popen([sys.executable, os.path.join(d, "server.py")], env=e); time.sleep(1.3); return p
procs = {"sol": start("sol"), "met": start("met")}
P = F = 0
def check(n, c, x=""):
    global P, F
    if c: P += 1; print("PASS", n)
    else: F += 1; print("FAIL", n, x)
def call(port, path, body=None, admin=None, secret=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15); h = {"Content-Type": "application/json"}
    if admin: h["X-Admin-Password"] = admin
    if secret: h["X-Federation-Secret"] = secret
    if cookie: h["Cookie"] = cookie
    c.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse(); raw = r.read(); ck = r.getheader("Set-Cookie")
    try: j = json.loads(raw)
    except Exception: j = raw
    return r.status, j, raw
S = lambda path, body=None, **k: call(SP, path, body, **k)
M = lambda path, body=None, **k: call(MP, path, body, **k)
def q(db, sql):
    c = sqlite3.connect(db, timeout=10); r = c.execute(sql).fetchall(); c.close(); return r
def users(): return (q(SDB, "select count(*) from users")[0][0], q(MDB, "select count(*) from users")[0][0])
def pins(): return {n: q(db, "select name_key, pin_hash from users order by 1") for n, db in (("s", SDB), ("m", MDB))}
def wait(cond, secs=8):
    t = time.time()
    while time.time() - t < secs:
        if cond(): return True
        time.sleep(0.25)
    return False
def mid(name): return [m["id"] for m in M("/api/admin/members", {}, admin="ma")[1]["members"] if m["name"] == name][0]
def sid(name): return [m["id"] for m in S("/api/admin/members", {}, admin="sa")[1]["members"] if m["name"] == name][0]
try:
    # ---- secret required
    for port, nm in ((SP, "solstice"), (MP, "metrics")):
        for path in ("/api/federation/verify", "/api/federation/exists", "/api/federation/set-pin"):
            s, j, _ = call(port, path, {"name": "x", "pin": "1234"}); s2, _, _ = call(port, path, {"name": "x"}, secret="wrong")
            check(f"{nm} {path}: 401 without/with wrong secret", s == 401 and s2 == 401)
    check("no accounts yet", users() == (0, 0))
    # ---- sign up on metrics, log in on solstice without any signup
    s, j, _ = M("/api/register", {"name": "Fed Alice", "pin": "1234"}); check("register on metrics", s == 200 and j["new"] is True)
    check("only metrics has the account", users() == (0, 1))
    s, j, _ = S("/api/login", {"name": "Fed Alice", "pin": "1234"}); check("login on solstice with no signup works", s == 200 and j["name"] == "Fed Alice", (s, j))
    check("solstice account created from the peer", users() == (1, 1))
    s, j, _ = S("/api/login", {"name": "fed ALICE", "pin": "1234"}); check("case-insensitive name", s == 200)
    check("still one account each (no duplicates)", users() == (1, 1))
    # ---- sign up on solstice, log in on metrics
    s, j, _ = S("/api/register", {"name": "Sol Bob", "pin": "4321"}); check("register on solstice", s == 200)
    s, j, _ = M("/api/login", {"name": "sol bob", "pin": "4321"}); check("login on metrics with no signup works (case-insens)", s == 200 and j["name"] == "Sol Bob", (s, j))
    check("data stays separate: metrics user has no solstice data", users() == (2, 2))
    # ---- failed logins never create accounts anywhere
    before = users()
    for port in (SP, MP):
        s, j, _ = call(port, "/api/login", {"name": "Nobody Here", "pin": "1234"}); check("unknown name -> generic message", s == 401 and "doesn't match" in j["error"] and "Please try again" in j["error"])
        s, j, _ = call(port, "/api/login", {"name": "Fed Alice", "pin": "9999"}); check("wrong PIN -> generic message", s == 401 and "doesn't match" in j["error"])
    check("failed logins created no accounts anywhere", users() == before, (users(), before))
    # ---- registration blocked by a name on the peer
    s, j, _ = S("/api/register", {"name": "SOL bob", "pin": "1111"}); check("local duplicate blocked (friendly)", s == 409 and "tap \"Log in\"" in j["error"])
    s, j, _ = M("/api/register", {"name": "Only Sol", "pin": "2222"}); check("setup: Only Sol on metrics", s == 200)
    s, j, _ = S("/api/register", {"name": "only sol", "pin": "3333"}); check("name taken on the peer blocks registration with friendly message", s == 409 and "already taken" in j["error"] and "same PIN" in j["error"] and "slightly different name" in j["error"], (s, j))
    check("no account created by blocked registration", users() == (2, 3), users())
    # ---- verify endpoint contents
    s, j, raw = M("/api/federation/verify", {"name": "fed alice", "pin": "1234"}, secret=SECRET)
    check("verify ok returns only ok/name/pin_changed", s == 200 and set(j) == {"ok", "name", "pin_changed"} and j["ok"] and j["name"] == "Fed Alice", j)
    check("verify never leaks hashes/salts", not re.search(rb"[0-9a-f]{32}", raw))
    s, j, raw = M("/api/federation/verify", {"name": "fed alice", "pin": "0000"}, secret=SECRET); check("verify wrong pin -> only ok:false", j == {"ok": False})
    s, j, _ = M("/api/federation/verify", {"name": "ghost", "pin": "0000"}, secret=SECRET); check("verify unknown -> only ok:false (same shape)", j == {"ok": False})
    s, j, _ = M("/api/federation/exists", {"name": "FED alice"}, secret=SECRET); check("exists true", j == {"exists": True})
    s, j, _ = M("/api/federation/exists", {"name": "ghost"}, secret=SECRET); check("exists false", j == {"exists": False})
    q(MDB, "update users set fails=0")
    # ---- PIN reset on metrics propagates to solstice (push), old PIN stops working
    s, j, _ = M("/api/admin/members/reset-pin", {"id": mid("Fed Alice"), "pin": "5555"}, admin="ma"); check("metrics admin has Reset PIN", s == 200 and j["federated"] is True, (s, j))
    check("reset pushed to the solstice account", wait(lambda: pins()["s"] == [r for r in pins()["s"]] and q(SDB, "select pin_hash from users where name_key='fed alice'")[0][0] == q(MDB, "select pin_hash from users where name_key='fed alice'")[0][0]))
    s, j, _ = S("/api/login", {"name": "Fed Alice", "pin": "5555"}); check("new PIN works on solstice", s == 200)
    s, j, _ = S("/api/login", {"name": "Fed Alice", "pin": "1234"}); check("old PIN fails on solstice", s == 401)
    s, j, _ = M("/api/login", {"name": "Fed Alice", "pin": "1234"}); check("old PIN fails on metrics", s == 401)
    q(SDB, "update users set fails=0"); q(MDB, "update users set fails=0")
    # ---- reset on solstice propagates to metrics
    s, j, _ = S("/api/admin/members/reset-pin", {"id": sid("Fed Alice"), "pin": "6666"}, admin="sa"); check("solstice admin Reset PIN", s == 200)
    check("push reached metrics", wait(lambda: q(SDB, "select pin_hash from users where name_key='fed alice'")[0][0] == q(MDB, "select pin_hash from users where name_key='fed alice'")[0][0]))
    s, j, _ = M("/api/login", {"name": "Fed Alice", "pin": "6666"}); check("new PIN works on metrics", s == 200)
    s, j, _ = M("/api/login", {"name": "Fed Alice", "pin": "5555"}); check("old PIN fails on metrics", s == 401)
    check("outbox drained", wait(lambda: q(SDB, "select count(*) from fed_outbox")[0][0] == 0))
    q(MDB, "update users set fails=0")
    # ---- peer down while resetting: lazy pull + retry
    procs["met"].terminate(); procs["met"].wait()
    s, j, _ = S("/api/admin/members/reset-pin", {"id": sid("Fed Alice"), "pin": "7777"}, admin="sa"); check("reset works while peer is down", s == 200)
    time.sleep(2.2); check("reset is queued for retry", q(SDB, "select count(*) from fed_outbox")[0][0] == 1)
    procs["met"] = start("met")
    s, j, _ = M("/api/login", {"name": "Fed Alice", "pin": "7777"})
    check("login on metrics right after restart works (lazy: newer pin_changed from peer)", s == 200, (s, j))
    check("retry eventually empties the outbox", wait(lambda: q(SDB, "select count(*) from fed_outbox")[0][0] == 0))
    s, j, _ = M("/api/login", {"name": "Fed Alice", "pin": "6666"}); check("old PIN no longer works on metrics", s == 401)
    q(MDB, "update users set fails=0"); q(SDB, "update users set fails=0")
    # ---- stale peer cannot overwrite a newer local PIN
    s, j, _ = M("/api/federation/set-pin", {"name": "fed alice", "salt": "a" * 32, "pin_hash": "b" * 64, "ts": 1.0}, secret=SECRET); check("older set-pin ignored", j == {"applied": False})
    s, j, _ = M("/api/federation/set-pin", {"name": "ghost", "salt": "a" * 32, "pin_hash": "b" * 64, "ts": time.time()}, secret=SECRET); check("set-pin never creates accounts", j == {"applied": False} and users()[1] == 3)
    s, j, _ = M("/api/federation/set-pin", {"name": "fed alice", "salt": "zz", "pin_hash": "b", "ts": time.time()}, secret=SECRET); check("malformed set-pin -> 400", s == 400)
    # ---- peer down: local login still fine, friendly message, registration fails closed
    procs["sol"].terminate(); procs["sol"].wait()
    t0 = time.time(); s, j, _ = M("/api/login", {"name": "Fed Alice", "pin": "7777"}); check("local login works with peer down (fast)", s == 200 and time.time() - t0 < 3)
    t0 = time.time(); s, j, _ = M("/api/login", {"name": "Brand New", "pin": "7777"}); check("unknown name with peer down: generic + friendly hint, within timeout", s == 401 and "doesn't match" in j["error"] and "can't check" in j["error"] and time.time() - t0 < 4)
    n = users()[1]; s, j, _ = M("/api/register", {"name": "Brand New", "pin": "7777"}); check("registration with peer down: friendly 503, no account", s == 503 and "try again in a few minutes" in j["error"] and users()[1] == n, (s, j))
    procs["sol"] = start("sol")
    # ---- lockout cannot be bypassed via federation
    S("/api/login", {"name": "Fed Alice", "pin": "7777"}); q(SDB, "update users set fails=0,locked_until=0"); q(MDB, "update users set fails=0,locked_until=0")
    codes = [S("/api/login", {"name": "Fed Alice", "pin": "0001"})[0] for _ in range(5)]
    check("5 wrong tries on solstice -> 401s", codes == [401] * 5, codes)
    s, j, _ = S("/api/login", {"name": "Fed Alice", "pin": "7777"}); check("solstice locked even with the right PIN", s == 429 and "any coach" in j["error"])
    s, j, _ = M("/api/login", {"name": "Fed Alice", "pin": "7777"}); check("peer verify counted the same failures: metrics locked too (no bypass)", s == 429, (s, j))
    s, j, _ = M("/api/federation/verify", {"name": "fed alice", "pin": "7777"}, secret=SECRET); check("locked name returns not-ok without checking", j == {"ok": False})
    # brute force through the other site for an account that only exists on one app
    s, j, _ = S("/api/register", {"name": "Lock Test", "pin": "2468"}); check("setup Lock Test on solstice", s == 200)
    codes = [M("/api/login", {"name": "Lock Test", "pin": "000%d" % i})[0] for i in range(5)]
    check("5 wrong tries through metrics: all rejected, no account created there", codes == [401] * 5 and not q(MDB, "select 1 from users where name_key='lock test'"))
    s, j, _ = M("/api/login", {"name": "Lock Test", "pin": "2468"}); check("right PIN via metrics now refused (peer locked the name)", s == 401, (s, j))
    s, j, _ = S("/api/login", {"name": "Lock Test", "pin": "2468"}); check("solstice also locked (counted via federation)", s == 429)
    check("still no metrics account for Lock Test", not q(MDB, "select 1 from users where name_key='lock test'"))
    # admin reset clears locks on both
    s, j, _ = S("/api/admin/members/reset-pin", {"id": sid("Lock Test"), "pin": "1357"}, admin="sa"); check("admin reset", s == 200)
    s, j, _ = S("/api/login", {"name": "Lock Test", "pin": "1357"}); check("unlocked by reset", s == 200)
    # ---- repeated bad secrets lock the federation door (rate limit)
    codes = [M("/api/federation/verify", {"name": "x", "pin": "1"}, secret="bad")[0] for _ in range(10)]
    check("bad-secret attempts are rate limited (429 after repeated 401)", 401 in codes and 429 in codes, codes)
    # ---- delete removes only the local account
    s, j, _ = M("/api/admin/members/reset-pin", {"id": 9999, "pin": "1234"}, admin="ma"); check("reset unknown member 404", s == 404)
    s, j, _ = M("/api/admin/members/reset-pin", {"id": 1, "pin": "12"}, admin="ma"); check("reset needs 4 digits", s == 400)
    check("admin members endpoints need password", M("/api/admin/members/reset-pin", {"id": 1, "pin": "1234"})[0] == 401 and M("/api/admin/members/delete", {"id": 1, "confirm": "x"})[0] == 401)
    s, j, _ = M("/api/admin/members/delete", {"id": mid("Sol Bob"), "confirm": "wrong"}, admin="ma"); check("delete needs the exact name", s == 400)
    s, j, _ = M("/api/admin/members/delete", {"id": mid("Sol Bob"), "confirm": "sol bob"}, admin="ma"); check("admin delete member (metrics)", s == 200 and not q(MDB, "select 1 from users where name_key='sol bob'") and q(SDB, "select 1 from users where name_key='sol bob'"))
    # ---- Time Tracker needs 3 different timed activities; Self Love needs Supine Get-ups too
    def login_cookie(name, pin):
        c = http.client.HTTPConnection("127.0.0.1", SP, timeout=15)
        c.request("POST", "/api/login", json.dumps({"name": name, "pin": pin}), {"Content-Type": "application/json"})
        r = c.getresponse(); body = r.read(); h = r.getheader("Set-Cookie"); assert h, (r.status, body); return h.split(";")[0]
    def aid(name): return [a["id"] for a in S("/api/admin/list", {}, admin="sa")[1]["activities"] if a["name"] == name][0]
    for nm in ("10min Plank", "10min Wall Sit", "10min Hang (supported)", "100 Supine Get-ups (Stand)"):
        S("/api/admin/save", {"name": nm, "target": 10 if "min" in nm else 100, "unit": "minutes" if "min" in nm else "reps", "phase": 2}, admin="sa")
    ck = login_cookie("Lock Test", "1357")
    def me(): return S("/api/me", cookie=ck)[1]
    def tt(): return [b for b in me()["badgeList"] if b["key"] == "timed"][0]
    check("Time Tracker desc says 3 timed activities", tt()["desc"] == "Complete 3 timed activities" and tt()["hint"] == "0 of 3 timed activities done", tt())
    s, j, _ = S("/api/progress", {"activity_id": aid("10min Plank"), "amount": 10}, cookie=ck)
    check("1 timed activity: Time Tracker still locked", s == 200 and not tt()["got"] and tt()["hint"] == "1 of 3 timed activities done", tt())
    S("/api/progress", {"activity_id": aid("10min Wall Sit"), "amount": 10}, cookie=ck)
    check("2 timed activities: still locked", not tt()["got"] and tt()["hint"] == "2 of 3 timed activities done")
    s, j, _ = S("/api/progress", {"activity_id": aid("10min Hang (supported)"), "amount": 10}, cookie=ck)
    check("3 different timed activities: Time Tracker earned", tt()["got"] and any(b["key"] == "timed" for b in j.get("newBadges", [])), j.get("newBadges"))
    S("/api/progress", {"activity_id": aid("100 Supine Get-ups (Stand)"), "amount": 100}, cookie=ck)
    check("non-timed activity is not counted as timed", tt()["got"] and [b for b in me()["badgeList"] if b["key"] == "count10"][0]["hint"] == "4 of 10 activities done")
    sl = [b for b in me()["badgeList"] if b["key"] == "selflove"][0]
    check("Self Love desc mentions Supine Get-ups", "Supine Get-ups" in sl["desc"] and not sl["got"], sl)
    # ---- Strong Arms: medicine ball throw matches MedBall / Med Ball / Medicine Ball (any case)
    for nm in ("100 FitFighter Overhead Slam", "100 ViPR Curl Overhead Press", "1,000 MACE 360/Plate Halo"):
        S("/api/admin/save", {"name": nm, "target": 1, "unit": "reps", "phase": 1}, admin="sa")
        S("/api/progress", {"activity_id": aid(nm), "amount": 1}, cookie=ck)
    S("/api/admin/save", {"name": "1,000 Medicine Ball Throw", "target": 1, "unit": "reps", "phase": 2}, admin="sa")
    def arms(): return [b for b in me()["badgeList"] if b["key"] == "arms"][0]["got"]
    check("Strong Arms locked until the ball throw is done", not arms())
    mb = aid("1,000 Medicine Ball Throw"); S("/api/progress", {"activity_id": mb, "amount": 1}, cookie=ck)
    check("Strong Arms earned with 'Medicine Ball Throw'", arms())
    for nm in ("1,000 Med Ball Throw", "1,000 MEDBALL THROW", "1,000 med ball throw"):
        S("/api/admin/save", {"id": mb, "name": nm, "target": 1, "unit": "reps", "phase": 2}, admin="sa")
        S("/api/progress/undo", {"activity_id": mb}, cookie=ck); off = arms()
        S("/api/progress", {"activity_id": mb, "amount": 1}, cookie=ck)
        check("Strong Arms re-earned when named %r" % nm, not off and arms())
    S("/api/admin/save", {"id": mb, "name": "1,000 Ball Toss", "target": 1, "unit": "reps", "phase": 2}, admin="sa")
    S("/api/progress/undo", {"activity_id": mb}, cookie=ck); S("/api/progress", {"activity_id": mb, "amount": 1}, cookie=ck)
    check("unrelated name does not satisfy Strong Arms", not arms())
    # ---- new 2m Practice requirements for Self Love / Stability Master / Miles & Smiles
    def badge(k): return [b for b in me()["badgeList"] if b["key"] == k][0]
    check("Self Love desc lists Grounding and Box Breath", "Grounding" in badge("selflove")["desc"] and "Box Breath" in badge("selflove")["desc"])
    check("Stability Master desc lists Ankle Rockers", "Ankle Rockers" in badge("rock")["desc"])
    check("Stability Master desc lists Walk Backward; Miles & Smiles unchanged", "Walk Backward" in badge("rock")["desc"] and "Walk Backward" not in badge("miles")["desc"])
    st = ("10min Plank", "10min Wall Sit", "Single Leg Balance (10 min each leg)", "10min Hang (supported)")
    S("/api/admin/save", {"name": st[2], "target": 20, "unit": "minutes", "phase": 2}, admin="sa")
    S("/api/progress", {"activity_id": aid(st[2]), "amount": 20}, cookie=ck)
    S("/api/admin/save", {"name": "2m Arms Out Ankle Rockers", "target": 1, "unit": "x", "phase": 3}, admin="sa")
    S("/api/progress", {"activity_id": aid("10min Plank"), "amount": 1}, cookie=ck)
    check("Stability Master locked without Ankle Rockers/Walk Backward", not badge("rock")["got"])
    s, j, _ = S("/api/progress", {"activity_id": aid("2m Arms Out Ankle Rockers"), "amount": 1}, cookie=ck)
    check("Stability Master still locked without Walk Backward", not badge("rock")["got"])
    S("/api/admin/save", {"name": "2m Walk Backward", "target": 1, "unit": "x", "phase": 3}, admin="sa")
    s, j, _ = S("/api/progress", {"activity_id": aid("2m Walk Backward"), "amount": 1}, cookie=ck)
    check("Stability Master earned once Ankle Rockers and Walk Backward done", badge("rock")["got"], j.get("newBadges"))
finally:
    for p in procs.values(): p.terminate()
print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)
