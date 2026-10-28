"""Member nutrition profile + macro/water estimates. Pure functions (no DB), so they are easy to test.
Formulas: Mifflin-St Jeor BMR (Katch-McArdle when body fat % is given) x activity factor = TDEE, then a goal adjustment."""
import re
DISCLAIMER = "These are estimates, not medical advice."
DIETITIAN_MSG = ("Because of the health flag on your profile, we don't show a high-protein target. "
                 "Please check with your doctor or Gymnazo's dietitian, Brittany Perry, before changing your protein or fluid intake.")
ACTIVITY = {"sedentary":(1.2,"Mostly sitting"),"light":(1.375,"Light: on your feet some, 1-3 workouts a week"),
            "moderate":(1.55,"Moderate: 3-5 workouts a week"),"very":(1.725,"Very active: 6-7 hard workouts a week"),
            "extra":(1.9,"Extra active: physical job plus training")}
GOALS = {"loss":"Weight loss","bulk":"Hypertrophy - Bulking","longevity":"Hypertrophy - Longevity/muscle for health","maintain":"Maintain"}
# calorie adjustment by goal and pace
ADJ = {"loss":{"slow":-0.15,"moderate":-0.20},"bulk":{"slow":0.10,"moderate":0.15},"longevity":{"slow":0.0,"moderate":0.05},"maintain":{"slow":0.0,"moderate":0.0}}
# protein g/kg: (low, high, target)
PROTEIN = {"loss":(1.8,2.2,2.0),"bulk":(1.6,2.2,1.9),"longevity":(1.6,2.0,1.8),"maintain":(1.6,2.0,1.8)}
FAT_PCT, FAT_FLOOR = 0.275, 0.6
LB, IN = 0.45359237, 2.54
CHOICES = {"units":("imperial","metric"),"sex":("female","male","other"),"activity":tuple(ACTIVITY),"goal":tuple(GOALS),
           "pace":("slow","moderate"),"sweat":("light","average","heavy")}

def _num(v, lo, hi, name, err, required=False):
    if v in (None, ""):
        if required: err.append("Please enter your %s." % name)
        return None
    try: x = float(v)
    except Exception: x = float("nan")
    if not (x == x and lo <= x <= hi): err.append("%s should be a number from %g to %g." % (name[0].upper()+name[1:], lo, hi)); return None
    return round(x, 2)
def _txt(v, n=300): return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(v or ""))).strip()[:n]
def _bool(v): return v is True or str(v).lower() in ("1","true","yes","on")

def clean(b):
    """Validate a submitted profile. Returns (profile_dict, [errors]). Weight/height are stored in kg/cm; the unit choice is kept for display."""
    err, p = [], {}
    for k, opts in CHOICES.items():
        v = str(b.get(k) or "").strip().lower()
        p[k] = v if v in opts else {"units":"imperial","sex":"","activity":"","goal":"","pace":"moderate","sweat":"average"}[k]
    for k in ("sex","activity","goal"):
        if not p[k]: err.append("Please choose your %s." % {"sex":"sex","activity":"activity level","goal":"goal"}[k])
    imp = p["units"] == "imperial"
    if imp:
        w = _num(b.get("weight"), 66, 900, "weight (lbs)", err, True)
        gw = _num(b.get("goal_weight"), 66, 900, "goal weight (lbs)", err)
        ft = _num(b.get("height_ft"), 3, 8, "height (feet)", err, True); inch = _num(b.get("height_in"), 0, 11.99, "height (inches)", err) or 0
        p["weight_kg"] = round(w*LB, 2) if w else None; p["goal_weight_kg"] = round(gw*LB, 2) if gw else None
        p["height_cm"] = round((ft*12+inch)*IN, 1) if ft else None
    else:
        p["weight_kg"] = _num(b.get("weight"), 30, 400, "weight (kg)", err, True)
        p["goal_weight_kg"] = _num(b.get("goal_weight"), 30, 400, "goal weight (kg)", err)
        p["height_cm"] = _num(b.get("height_cm"), 90, 250, "height (cm)", err, True)
    a = _num(b.get("age"), 13, 110, "age", err, True); p["age"] = int(a) if a else None
    t = _num(b.get("training_days"), 0, 7, "training days per week", err); p["training_days"] = int(t) if t is not None else 3
    m = _num(b.get("meals_per_day"), 1, 8, "meals per day", err); p["meals_per_day"] = int(m) if m else 3
    p["body_fat_pct"] = _num(b.get("body_fat_pct"), 3, 70, "body fat %", err)
    p["caffeine_cups"] = _num(b.get("caffeine_cups"), 0, 20, "caffeine (cups/day)", err) or 0
    p["alcohol_drinks"] = _num(b.get("alcohol_drinks"), 0, 50, "alcohol (drinks/week)", err) or 0
    p["trains_in_heat"] = _bool(b.get("trains_in_heat"))
    p["pregnant"] = _bool(b.get("pregnant")); p["kidney"] = _bool(b.get("kidney"))
    p["other_condition"] = _txt(b.get("other_condition"), 200); p["restrictions"] = _txt(b.get("restrictions"), 300)
    return p, err

def calculate(p):
    """Recommendations for a cleaned profile. Returns a dict of numbers + notes."""
    w, h, a, sex = p["weight_kg"], p["height_cm"], p["age"], p["sex"]
    bf = p.get("body_fat_pct"); lbm = w*(1-bf/100) if bf else None
    if lbm: bmr, bmr_f = 370 + 21.6*lbm, "Katch-McArdle"
    else:
        s = {"male":5, "female":-161}.get(sex, -78)  # "other": midpoint of the two
        bmr, bmr_f = 10*w + 6.25*h - 5*a + s, "Mifflin-St Jeor"
    fac = ACTIVITY[p["activity"]][0]; tdee = bmr*fac
    goal, pace = p["goal"], p["pace"]
    flag = p.get("pregnant") or p.get("kidney") or bool(p.get("other_condition"))
    adj = ADJ[goal][pace]; notes = []
    if p.get("pregnant") and adj < 0:
        adj = 0.0; notes.append("No calorie deficit is shown while pregnant or breastfeeding.")
    cal = tdee*(1+adj)
    floor = 1500 if sex == "male" else 1200
    if cal < floor: cal = floor; notes.append("Calories are held at a %d kcal minimum." % floor)
    cal = round(cal/10)*10
    out = {"bmr":round(bmr), "bmr_formula":bmr_f, "activity_factor":fac, "tdee":round(tdee), "adjustment_pct":round(adj*100),
           "calories":cal, "lean_mass_kg":round(lbm,1) if lbm else None, "flagged":bool(flag), "notes":notes, "disclaimer":DISCLAIMER}
    fat = max(cal*FAT_PCT/9, FAT_FLOOR*w)
    if flag:
        out.update(protein_g=None, protein_per_meal_g=None, carbs_g=None, fat_g=round(fat), medical_message=DIETITIAN_MSG)
    else:
        lo, hi, tg = PROTEIN[goal]
        gw = p.get("goal_weight_kg"); basis, bname = w, "current weight"
        if goal == "loss":
            if lbm and gw: basis, bname = max(lbm/0.8, gw if gw < w else w), "lean-mass-based weight"
            elif gw and gw < w: basis, bname = gw, "goal weight"
        elif goal in ("longevity","maintain") and lbm and lbm/0.85 < w: basis, bname = lbm/0.85, "lean-mass-based weight"
        basis = min(basis, w)
        prot = basis*tg
        carbs = (cal - prot*4 - fat*9)/4
        if carbs < 0: carbs = 0; notes.append("Protein and fat use up the calorie target, so carbs show as 0. Talk with a coach about this.")
        out.update(protein_g=round(prot), protein_range_g=[round(basis*lo), round(basis*hi)], protein_g_per_kg=tg, protein_basis=bname,
                   protein_basis_kg=round(basis,1), protein_per_meal_g=round(prot/p["meals_per_day"]), fat_g=round(fat), carbs_g=round(carbs))
    base = 35*w; td = 500 + (500 if p["sweat"] == "heavy" else 0) + (500 if p.get("trains_in_heat") else 0)
    rest_ml = round(base/50)*50; train_ml = round((base+td)/50)*50
    wn = ["Add about 500 ml (17 oz) for each hour of training; more if you sweat a lot or train in heat."]
    if p["caffeine_cups"] >= 3 or p["alcohol_drinks"] > 0:
        wn.append("You drink caffeine/alcohol: add a glass of water for each alcoholic drink, and a bit extra on high-caffeine days.")
    else: wn.append("Add a bit more on days with lots of caffeine or any alcohol.")
    if p.get("kidney"): wn.insert(0, "With kidney disease, fluid needs can be different. Follow your doctor's fluid advice over this estimate.")
    out["water"] = {"rest_day_ml":rest_ml, "training_day_ml":train_ml, "rest_day_oz":round(rest_ml/29.5735), "training_day_oz":round(train_ml/29.5735),
                    "training_days":p["training_days"], "weekly_avg_ml":round((rest_ml*(7-p["training_days"]) + train_ml*p["training_days"])/7/50)*50, "notes":wn}
    return out
