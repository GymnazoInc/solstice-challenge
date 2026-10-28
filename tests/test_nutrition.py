"""Nutrition profile + macro/water calculator tests (unit + HTTP). Usage: python3 tests/test_nutrition.py"""
import sys, os, subprocess, time, json, http.client, tempfile, sqlite3, shutil
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, HERE)
import nutrition as N
P = F = 0
def check(n, c, x=""):
    global P, F
    if c: P += 1; print("PASS", n)
    else: F += 1; print("FAIL", n, x)
def prof(**k):
    b = dict(units="metric", weight=80, height_cm=180, age=30, sex="male", activity="moderate", training_days=4, goal="maintain", pace="moderate", meals_per_day=4)
    b.update(k); p, e = N.clean(b); assert not e, e; return p
# ---- unit: formulas
r = N.calculate(prof())
check("Mifflin male BMR", r["bmr"] == 1780 and r["bmr_formula"] == "Mifflin-St Jeor", r["bmr"])   # 800+1125-150+5
check("TDEE = BMR x 1.55", r["tdee"] == round(1780*1.55))
check("maintain = TDEE (rounded to 10)", r["calories"] == round(1780*1.55/10)*10)
rf = N.calculate(prof(sex="female")); check("Mifflin female BMR", rf["bmr"] == 1614)
rk = N.calculate(prof(body_fat_pct=20)); check("Katch-McArdle when body fat given", rk["bmr_formula"] == "Katch-McArdle" and rk["bmr"] == round(370+21.6*64) and rk["lean_mass_kg"] == 64)
t = 1780*1.55
check("weight loss slow -15%", N.calculate(prof(goal="loss", pace="slow"))["calories"] == round(t*0.85/10)*10)
check("weight loss moderate -20%", N.calculate(prof(goal="loss"))["calories"] == round(t*0.80/10)*10)
check("bulk slow +10% / moderate +15%", N.calculate(prof(goal="bulk", pace="slow"))["calories"] == round(t*1.10/10)*10 and N.calculate(prof(goal="bulk"))["calories"] == round(t*1.15/10)*10)
check("longevity +5% (moderate) / maintenance (slow)", N.calculate(prof(goal="longevity"))["calories"] == round(t*1.05/10)*10 and N.calculate(prof(goal="longevity", pace="slow"))["calories"] == round(t/10)*10)
rb = N.calculate(prof(goal="bulk")); check("bulk protein 1.9 g/kg in 1.6-2.2 range", rb["protein_g"] == 152 and rb["protein_range_g"] == [128, 176])
rl = N.calculate(prof(goal="longevity")); check("longevity protein 1.6-2.0", rl["protein_range_g"] == [128, 160] and rl["protein_g"] == 144)
rw = N.calculate(prof(goal="loss", goal_weight=70)); check("loss protein 2.0 g/kg of goal weight", rw["protein_g"] == 140 and rw["protein_basis"] == "goal weight" and rw["protein_range_g"] == [126, 154])
check("loss protein uses current weight without goal", N.calculate(prof(goal="loss"))["protein_g"] == 160)
check("protein per meal = protein / meals", rb["protein_per_meal_g"] == round(152/4))
check("fat ~27.5% of calories", r["fat_g"] == round(r["calories"]*0.275/9))
check("macros add up to calories", abs(r["protein_g"]*4 + r["carbs_g"]*4 + r["fat_g"]*9 - r["calories"]) <= 15)
lo = N.calculate(prof(weight=150, height_cm=150, age=70, sex="female", activity="sedentary", goal="loss"))
check("fat floor 0.6 g/kg", lo["fat_g"] >= 0.6*150 - 1, lo["fat_g"])
check("calorie floor 1200 for women", lo["calories"] >= 1200)
check("water 35 ml/kg on rest days", r["water"]["rest_day_ml"] == 2800 and r["water"]["training_day_ml"] == 3300)
hw = N.calculate(prof(sweat="heavy", trains_in_heat=True))["water"]; check("heavy sweat + heat add water", hw["training_day_ml"] == 4300)
check("caffeine/alcohol water note", any("alcohol" in n for n in N.calculate(prof(alcohol_drinks=4))["water"]["notes"]))
check("disclaimer present", r["disclaimer"] == "These are estimates, not medical advice.")
for flag in (dict(pregnant=True), dict(kidney=True), dict(other_condition="Gout")):
    x = N.calculate(prof(goal="bulk", **flag))
    check("medical flag %s hides protein, shows Brittany Perry message" % list(flag)[0], x["protein_g"] is None and x["carbs_g"] is None and "Brittany Perry" in x["medical_message"])
check("pregnant: no deficit", N.calculate(prof(sex="female", goal="loss", pregnant=True))["adjustment_pct"] == 0)
check("no flags: no medical message", "medical_message" not in r)
# ---- unit: units + validation
pi, e = N.clean(dict(units="imperial", weight=176.4, height_ft=5, height_in=11, age=30, sex="male", activity="moderate", goal="maintain"))
check("imperial converts to kg/cm", not e and abs(pi["weight_kg"]-80.01) < 0.05 and abs(pi["height_cm"]-180.3) < 0.1)
check("US units are the default", N.clean({})[0]["units"] == "imperial")
_, e = N.clean({}); check("missing required fields rejected", len(e) >= 5)
_, e = N.clean(dict(units="metric", weight="abc", height_cm=999, age=5, sex="x", activity="moderate", goal="loss")); check("bad values rejected", len(e) >= 4)
check("html stripped from text", N.clean(dict(restrictions="<b>nuts</b>"))[0]["restrictions"] == "nuts")
# ---- HTTP
tmp = tempfile.mkdtemp(); PORT = 18131; DB = os.path.join(tmp, "s.db")
env = dict(os.environ, PORT=str(PORT), DB_PATH=DB, HOST="127.0.0.1", ADMIN_PASSWORD="sa", SOL_TEST_TODAY="2026-10-25", FEDERATION_SECRET="", PEER_URL="")
pr = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=env); time.sleep(1.3)
def call(path, body=None, cookie=None, admin=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15); h = {"Content-Type": "application/json"}
    if cookie: h["Cookie"] = cookie
    if admin: h["X-Admin-Password"] = admin
    c.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse(); raw = r.read(); ck = r.getheader("Set-Cookie")
    try: j = json.loads(raw)
    except Exception: j = raw
    return r.status, j, raw, ck
try:
    s, _, raw, _ = call("/nutrition"); check("GET /nutrition page 200", s == 200 and b"nutrition-v1" in raw and b"not medical advice" in raw)
    check("no navy in nutrition page", b"navy" not in raw.lower() and b"0f1b3d" not in raw.lower())
    s, _, raw, _ = call("/"); check("home page still loads + links to nutrition", s == 200 and b"/nutrition" in raw)
    check("API requires login", call("/api/nutrition")[0] == 401 and call("/api/nutrition", {})[0] == 401)
    s, j, _, ck = call("/api/register", {"name": "Nut Tester", "pin": "1234"}); ck = ck.split(";")[0]
    check("register ok", s == 200)
    s, j, _, _ = call("/api/nutrition", cookie=ck); check("empty profile at first", s == 200 and j["profile"] is None)
    s, j, _, _ = call("/api/nutrition", {"units": "imperial", "weight": "x"}, cookie=ck); check("invalid save -> 400 friendly error", s == 400 and j["error"])
    body = dict(units="imperial", weight=180, height_ft=5, height_in=10, age=40, sex="female", activity="light", training_days=3, goal="loss", pace="slow", goal_weight=160, meals_per_day=3, restrictions="no dairy")
    s, j, _, _ = call("/api/nutrition", body, cookie=ck)
    check("save profile -> results", s == 200 and j["results"]["calories"] > 1200 and j["results"]["protein_per_meal_g"] > 0 and j["profile"]["restrictions"] == "no dairy")
    s, j2, _, _ = call("/api/nutrition", cookie=ck); check("profile persists", j2["profile"]["weight_kg"] == j["profile"]["weight_kg"] and j2["results"] == j["results"])
    s, j, _, _ = call("/api/nutrition", dict(body, kidney=True), cookie=ck); check("kidney flag via API hides protein", j["results"]["protein_g"] is None and "Brittany Perry" in j["results"]["medical_message"])
    s, j, _, _ = call("/api/me", cookie=ck); check("existing /api/me still works", s == 200)
    check("one row stored", sqlite3.connect(DB).execute("select count(*) from nutrition_profiles").fetchone()[0] == 1)
    s, _, raw, _ = call("/beta/nutrition"); check("beta nutrition page", s == 200 and b"window.BETA=1" in raw)
    uid = [m["id"] for m in call("/api/admin/members", {}, admin="sa")[1]["members"] if m["name"] == "Nut Tester"][0]
    call("/api/admin/members/delete", {"id": uid, "user_id": uid, "confirm": "Nut Tester"}, admin="sa")
    check("member deleted", sqlite3.connect(DB).execute("select count(*) from users").fetchone()[0] == 0)
    check("member delete also removes nutrition row", sqlite3.connect(DB).execute("select count(*) from nutrition_profiles").fetchone()[0] == 0)
finally:
    pr.terminate()
# ---- migration on a copy of an existing DB keeps data
ex = os.path.join(tmp, "existing.db"); c0 = sqlite3.connect(ex)
c0.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, name_key TEXT UNIQUE, salt TEXT, pin_hash TEXT, created REAL, fails INTEGER DEFAULT 0, locked_until REAL DEFAULT 0);"
  "CREATE TABLE checkins(user_id INTEGER, day TEXT, note TEXT, created REAL, PRIMARY KEY(user_id, day));"
  "CREATE TABLE activities(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, target INTEGER, unit TEXT, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1);"
  "INSERT INTO users(id,name,name_key) VALUES(1,'Old Member','old member');INSERT INTO checkins VALUES(1,'2026-10-22','hi',1);INSERT INTO activities(name,target,unit) VALUES('Jumps',1000,'jumps');"); c0.commit(); c0.close()
if True:
    before = {t: sqlite3.connect(ex).execute("select count(*) from %s" % t).fetchone()[0] for t in ("users", "checkins", "activities")}
    pr = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")], env=dict(env, DB_PATH=ex, PORT=str(PORT+1))); time.sleep(1.3); pr.terminate(); pr.wait()
    c = sqlite3.connect(ex); after = {t: c.execute("select count(*) from %s" % t).fetchone()[0] for t in before}
    check("migration on existing DB adds nutrition table, keeps rows", after == before and c.execute("select count(*) from nutrition_profiles").fetchone()[0] == 0)
print("\n%d passed, %d failed" % (P, F)); sys.exit(1 if F else 0)
