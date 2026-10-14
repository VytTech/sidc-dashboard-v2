"""V2 – build the director dashboard from raw Ragic JSON (separate from the production build.py).

  py -I v2/build.py <raw_dir> <history_dir> <out_html>

Baseline logic reused unchanged from the production build: labour-cost formula and rates,
yard grouping (site_of), country detection, worker codes, encrypted history storage.

V2 additions: exclusion rules, position groups, timesheet-based FTE staffing, request statuses,
timesheet checks (no entries last week, location mismatch), monthly hours cube, categorised changes.

Environment: DATA_KEY / PAGE_PASSWORD (history encryption + worker codes, as in production),
             MGMT_PASSWORD (management-view password, only its SHA-256 goes into the page),
             RAGIC_BASE / RAGIC_ACCOUNT (source links), BUILD_DATE (testing).
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

STATUS_ORDER = ["Yes", "Workshop", "Scheduled", "Home"]

# ---------- V2 business rules ----------
EXCLUDED_LOCATIONS = {"HOME", "Migration", "Multiple location", "N/A2", "(unassigned)", "CBC"}
EXCLUDED_POSITIONS = {"Coordinator"}
UNASSIGNED = "__unassigned__"   # active, at home, no project: shown as unassigned (available) resources
INTERNAL_TIMESHEETS = {"Naujas cechas", "Kaminera", "Dubysa", "Laivytė"}  # separate timesheet systems
POSITION_GROUPS = {
    "Welders": ["Welder", "Welder TIG"],
    "SAW welders": ["Welder SAW"],
    "Ship builders": ["Ship builder", "Fitter"],
    "Supervision": ["Foreman", "Team leader"],
    "Management": ["Manager"],
    "Pipe fitters": ["Pipe fitter"],
    "Support": ["Warehouseman", "Helper"],
    "Quality": ["QC"],
    "Operators": ["Crane operator", "Plasma operator"],
    "Other trades": ["Painter", "Electrician", "Grinder", "Mechanic", "Fairier"],
}
GROUP_ORDER = list(POSITION_GROUPS)
GROUP_OF = {p: g for g, ps in POSITION_GROUPS.items() for p in ps}
FTE_HOURS = 40.0
WEEKS_SHOWN = 8          # current week + 7
COMPLETED_WEEKS = 4      # completed weeks used for repeated-shortage ranking
SHORTAGE_ALERT = 1.0     # FTE


def group_of(position):
    return GROUP_OF.get((position or "").strip(), "Other trades")


def excluded_location(name):
    return (name or "").strip() in EXCLUDED_LOCATIONS or not (name or "").strip()


# ---------- helpers (baseline) ----------

def num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def ragic_date(s):
    s = (s or "").strip()
    for fmt in ("%Y/%m/%d", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def year_of(v):
    m = re.search(r"\d{4}", str(v or ""))
    return m.group(0) if m else ""


def week_cell(v):
    m = re.match(r"\s*(\d+)", str(v or ""))
    n = int(m.group(1)) if m else 0
    return 0 if n == 53 else n


def site_of(project, names):
    """'NL-SPG-716' -> 'SPG' when SPG is itself a project (a yard running several projects)."""
    parts = project.split("-")
    if len(parts) >= 3 and parts[1] in names and parts[1] != project:
        return parts[1]
    return project


def person_name(p):
    return (p.get("First, Last names") or p.get("First&#44; Last names")
            or p.get("First, last names") or p.get("First&#44; last names") or "").strip()


def worker_code(position, key):
    """Stable pseudonym such as 'Welder · 3F9A' (keyed hash, the name cannot be recovered)."""
    secret = (os.environ.get("DATA_KEY") or os.environ.get("PAGE_PASSWORD") or "local-preview").encode()
    tag = hmac.new(secret, str(key).encode(), hashlib.sha256).hexdigest()[:4].upper()
    return f"{position or 'Worker'} · {tag}"


def monday(d):
    return d - dt.timedelta(days=d.weekday())


# ---------- history storage (baseline) ----------

def _fernet():
    secret = os.environ.get("DATA_KEY") or os.environ.get("PAGE_PASSWORD")
    if not secret:
        return None
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def load_history(hdir):
    f = _fernet()
    snaps = []
    for p in sorted(hdir.glob("*.json*")):
        try:
            raw = p.read_bytes()
            if p.suffix == ".enc":
                if not f:
                    continue
                raw = f.decrypt(raw)
            snaps.append(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            print(f"skip history file {p.name}: {exc}", file=sys.stderr)
    return snaps


def save_snapshot(hdir, snap):
    hdir.mkdir(parents=True, exist_ok=True)
    f = _fernet()
    data = json.dumps(snap, ensure_ascii=False).encode("utf-8")
    if f:
        (hdir / f"{snap['date']}.json.enc").write_bytes(f.encrypt(data))
    else:
        (hdir / f"{snap['date']}.json").write_bytes(data)


# ---------- labour cost (baseline, unchanged formula, rates and rules) ----------
GPM, SODRA_EMPLOYEE, SODRA_EMPLOYER = 0.20, 0.195, 0.0177
LT_FACTOR = 1 / (1 - GPM - SODRA_EMPLOYEE) * (1 + SODRA_EMPLOYER)
TRIP_SALARY_SHARE = 0.5   # ASSUMPTION (unchanged from production): half of net pay on trips is per diem
TRIP_FACTOR = TRIP_SALARY_SHARE * LT_FACTOR + (1 - TRIP_SALARY_SHARE)
SUBCONTRACTOR_RATE = 35.0
COUNTRY_ALIAS = {"FIN": "FI"}


class LabourCost:
    def __init__(self, raw, projects):
        self.rate = {}
        by_pos = defaultdict(list)
        for q in raw.get("qualification") or []:
            r = num(q.get("Hour rate"))
            if r > 0:
                self.rate[(q.get("Emploee") or "").strip()] = r
                by_pos[(q.get("Position") or "").strip()].append(r)
        self.pos_avg = {k: sum(v) / len(v) for k, v in by_pos.items()}
        allr = [r for v in by_pos.values() for r in v]
        self.all_avg = sum(allr) / len(allr) if allr else 0.0
        self.sub_company = {(c.get("Company") or "").strip(): (c.get("Subcontractor") or "") == "Yes"
                            for c in raw.get("companies") or []}
        self.sub_person = {person_name(p): (p.get("Subcontractor") or "") == "Yes" for p in raw.get("people") or []}
        self.country = {p["name"]: p["country"] for p in projects if p["country"]}
        self.names = set(self.country) | {p["name"] for p in projects}
        self.stats = Counter()

    def country_of(self, project):
        if project in self.country:
            return self.country[project]
        site = site_of(project, self.names)
        if site in self.country:
            return self.country[site]
        prefix = project.split("-")[0].upper()
        if 2 <= len(prefix) <= 3 and prefix.isalpha() and "-" in project:
            return COUNTRY_ALIAS.get(prefix, prefix)
        return "LT"

    def cost(self, project, position, hours, name, company):
        company = (company or "").strip()
        sub = self.sub_company.get(company) if company in self.sub_company else self.sub_person.get(name, False)
        if sub:
            self.stats["subcontractor"] += hours
            return hours * SUBCONTRACTOR_RATE
        rate = self.rate.get(name)
        if rate:
            self.stats["own_rate"] += hours
        else:
            rate = self.pos_avg.get(position) or self.all_avg
            self.stats["position_avg"] += hours
        trip = self.country_of(project) != "LT"
        self.stats["trip" if trip else "lt"] += hours
        return hours * rate * (TRIP_FACTOR if trip else LT_FACTOR)


# ---------- snapshot ----------

def months_between(first, last):
    out, (y, m) = [], (int(first[:4]), int(first[5:7]))
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return out


def build_snapshot(raw, today):
    year, week, _ = today.isocalendar()
    this_monday = monday(today)
    last_mon = this_monday - dt.timedelta(days=7)
    last_sun = this_monday - dt.timedelta(days=1)
    completed = [this_monday - dt.timedelta(days=7 * i) for i in range(COMPLETED_WEEKS, 0, -1)]  # oldest .. last
    shown = [this_monday + dt.timedelta(days=7 * i) for i in range(WEEKS_SHOWN)]
    wk = lambda d: d.isocalendar()[:2]

    # ---- projects (exclusions applied) and staffing plans per position/week
    projects, plans = [], {}
    for r in raw["projects"]:
        name = (r.get("Project") or "").strip()
        if not name or excluded_location(name) or (r.get("Active") or "Yes") != "Yes":
            continue
        projects.append({"name": name, "country": (r.get("Loc") or "").strip()})
        plan = defaultdict(dict)  # position -> {(isoyear, week): required}
        for row in (r.get("_subtable_1003520") or {}).values():
            pos = (row.get("Position") or "").strip()
            y = year_of(row.get("Year"))
            if not pos or not y or pos in EXCLUDED_POSITIONS:
                continue
            for w in range(1, 53):
                plan[pos][(int(y), w)] = week_cell(row.get(str(w)))
        if any(any(v.values()) for v in plan.values()):
            plans[name] = plan
    names = {p["name"] for p in projects}
    labour = LabourCost(raw, projects)

    companies_group = {(c.get("Company") or "").strip(): (c.get("PASSER GROUP Company") or "") == "Yes"
                       for c in raw.get("companies") or []}
    all_people = raw.get("people") or []
    main_position = {person_name(p): (p.get("Position") or "").strip() for p in all_people if person_name(p)}

    # ---- active workforce
    people = []
    for p in all_people:
        if (p.get("Active") or "").strip() != "Yes":
            continue
        st = (p.get("At work") or "").strip()
        proj = (p.get("Project") or "").strip() or "(unassigned)"
        pos = (p.get("Position") or "").strip()
        if st == "Terminate" or pos in EXCLUDED_POSITIONS:
            continue
        if proj == "(unassigned)" and st == "Home":
            # active people at home without a project = unassigned (available) resources, shown as their own pool
            proj = yard = UNASSIGNED
        elif excluded_location(proj):
            continue
        else:
            yard = site_of(proj, names)
            if excluded_location(yard):
                continue
        people.append({"name": person_name(p), "rid": p.get("_ragicId"), "project": proj, "yard": yard,
                       "country": "" if yard == UNASSIGNED else labour.country_of(proj), "position": pos or "Other", "group": group_of(pos),
                       "status": st if st in STATUS_ORDER else "Other",
                       "own": companies_group.get((p.get("Company") or "").strip(), False),
                       "company": (p.get("Company") or "").strip()})
    person_by_name = {p["name"]: p for p in people if p["name"]}

    # ---- one pass over timesheet rows
    today_s = today.isoformat()
    cube = defaultdict(lambda: [0.0, 0.0])              # (yard, project, group, position, month, own) -> [h, cost]
    yard_first = {}
    daily = defaultdict(lambda: defaultdict(float))      # name -> date -> h
    total_h = defaultdict(float)
    week_items = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))  # week -> name -> (proj, group, pos) -> h
    last_week_yards = defaultdict(set)                   # name -> yards with entries last completed week
    emp_yard_last = defaultdict(dict)                    # name -> yard -> last date
    cost_week = Counter()                                 # (isoyear, week) -> cost
    latest_entry, rows_used = "", 0
    # Yards without any Ragic timesheet row for 8 weeks keep their timesheets elsewhere:
    # timesheet-based checks would only create false alarms there, so they are skipped and shown as a data note.
    recent_from = (this_monday - dt.timedelta(days=56)).isoformat()
    yards_with_ts = set()
    for row in raw.get("hours") or []:
        sub, proj_t, pos_raw, h, day = row[:5]
        emp = ((row[5] if len(row) > 5 else "") or "").strip()
        company = ((row[6] if len(row) > 6 else "") or "").strip()
        day, hrs = ragic_date(day), num(h)
        if not day or day > today_s or not hrs:
            continue
        proj = (sub or "").strip() or (proj_t or "").strip() or "(no project)"
        yard = site_of(proj, names)
        if excluded_location(proj) or excluded_location(yard):
            continue
        pos_row = (pos_raw or "").strip()
        pos_eff = pos_row or main_position.get(emp, "") or "Other"
        if pos_eff in EXCLUDED_POSITIONS or (not pos_row and main_position.get(emp) in EXCLUDED_POSITIONS):
            continue
        cost = labour.cost(proj, pos_row or "Other", hrs, emp, company)   # baseline cost call
        grp = group_of(pos_eff)
        own = companies_group.get(company) if company in companies_group else \
            (person_by_name.get(emp) or {}).get("own", False)
        c = cube[(yard, proj, grp, pos_eff, day[:7], bool(own))]
        c[0] += hrs
        c[1] += cost
        yard_first[yard] = min(yard_first.get(yard, day[:7]), day[:7])
        rows_used += 1
        latest_entry = max(latest_entry, day)
        if day >= recent_from:
            yards_with_ts.add(yard)
        d = dt.date.fromisoformat(day)
        cost_week[wk(d)] += cost
        if emp:
            daily[emp][day] += hrs
            total_h[emp] += hrs
            emp_yard_last[emp][yard] = max(emp_yard_last[emp].get(yard, ""), day)
            if monday(d) in completed:
                week_items[monday(d)][emp][(proj, grp, pos_eff)] += hrs
            if last_mon <= d <= last_sun:
                last_week_yards[emp].add(yard)

    # ---- hours cube (months from the earliest yard start to the current month)
    first_month = min(yard_first.values()) if yard_first else today_s[:7]
    months = months_between(first_month, today_s[:7])
    midx = {m: i for i, m in enumerate(months)}
    hours_rows = [[k[0], k[1], k[2], k[3], midx[k[4]], int(k[5]), round(v[0], 1), round(v[1])]
                  for k, v in cube.items() if k[4] in midx]
    hours_total = sum(v[0] for v in cube.values())
    cost_total = sum(v[1] for v in cube.values())

    # ---- FTE per plan project from timesheets (completed weeks)
    def plan_key(proj):
        if proj in plans:
            return proj
        s = site_of(proj, names)
        return s if s in plans else None

    fte = {}  # week monday -> plan project -> {"g": {group: fte}, "p": {position: fte}}
    for mon in completed:
        acc = defaultdict(lambda: {"g": defaultdict(float), "p": defaultdict(float)})
        for emp, items in week_items[mon].items():
            H = sum(items.values())
            if H <= 0:
                continue
            share = min(H, FTE_HOURS) / FTE_HOURS
            for (proj, grp, pos), h in items.items():
                k = plan_key(proj)
                if k:
                    acc[k]["g"][grp] += share * h / H
                    acc[k]["p"][pos] += share * h / H
        fte[mon] = acc

    def plan_for(name, d):
        y, w = wk(d)
        by_pos = {pos: weeks.get((y, w), 0) for pos, weeks in plans[name].items()}
        by_grp = defaultdict(float)
        for pos, v in by_pos.items():
            by_grp[group_of(pos)] += v
        return by_pos, by_grp

    # ---- manpower requests
    conf = {}
    for r in raw.get("rotation") or []:
        n = person_name(r) or (r.get("F") or "").strip()
        if n and (n not in conf or (r.get("Active") == "Yes")):
            conf[n] = (r.get("Conf.") or "").strip() == "Yes"
    ragic = f"{os.environ.get('RAGIC_BASE', 'https://eu4.ragic.com').rstrip('/')}/{os.environ.get('RAGIC_ACCOUNT', 'SIDC10000')}"
    requests = []
    for r in raw.get("requests") or []:
        y, w = int(num(r.get("Year"))), int(num(r.get("Week")))
        proj = (r.get("Project") or "").strip()
        pos = (r.get("Position") or "").strip()
        if y != year or w < week - 4 or excluded_location(proj) or pos in EXCLUDED_POSITIONS:
            continue
        yard = site_of(proj, names)
        if excluded_location(yard):
            continue
        date = ragic_date(r.get("Date"))
        selected = []
        for row in (r.get("_subtable_1015956") or {}).values():
            n = person_name(row)
            if not n:
                continue
            last = emp_yard_last.get(n, {}).get(yard, "")
            working = bool(last) and (not date or last >= date)
            selected.append({"name": n, "position": (row.get("Position") or "").strip(),
                             "conf": conf.get(n, False), "working": working})
        requested = int(num(r.get("Number")))
        n_sel = len(selected)
        n_work = sum(1 for s in selected if s["working"])
        n_conf = sum(1 for s in selected if not s["working"] and s["conf"])
        if n_sel < requested:
            status = "still_needed"
        elif n_sel and n_work == n_sel:
            status = "likely_completed"
        elif n_work and n_work + n_conf == n_sel:
            status = "partly_working"
        elif n_sel and n_conf == n_sel:
            status = "team_reserved"
        else:
            status = "reservation_incomplete"
        requests.append({"id": r.get("_ragicId"), "url": f"{ragic}/planing/52/{r.get('_ragicId')}" if r.get("_ragicId") else "",
                         "project": proj, "yard": yard, "country": labour.country_of(proj), "position": pos,
                         "group": group_of(pos), "week": w, "date": date, "requested": requested,
                         "reserved": n_sel, "confirmed": n_conf, "working": n_work,
                         "still": max(0, requested - n_sel), "status": status, "people": selected})
    requests.sort(key=lambda r: (r["week"], r["yard"], r["project"]))

    # ---- staffing (plans are a separate process: never mixed with requests)
    staffing = []
    for name, plan in plans.items():
        country = labour.country_of(name)
        no_ts = name not in yards_with_ts and site_of(name, names) not in yards_with_ts
        hist = []
        for mon in completed:
            _, pg = plan_for(name, mon)
            act = fte[mon].get(name, {"g": {}, "p": {}})
            groups = sorted(set(pg) | set(act["g"]), key=GROUP_ORDER.index)
            shorts = {g: pg.get(g, 0) - act["g"].get(g, 0.0) for g in groups if pg.get(g, 0) > 0}
            hist.append({"week": wk(mon)[1], "plan": sum(pg.values()), "actual": round(sum(act["g"].values()), 2),
                         "below": not no_ts and any(v >= SHORTAGE_ALERT for v in shorts.values()),
                         "short": round(sum(max(0.0, v) for v in shorts.values()), 2)})
        last = completed[-1]
        pp, pg = plan_for(name, last)
        act = fte[last].get(name, {"g": {}, "p": {}})
        groups = []
        for g in GROUP_ORDER:
            plan_g, act_g = pg.get(g, 0), act["g"].get(g, 0.0)
            if not plan_g and not act_g:
                continue
            positions = sorted({p for p in pp if group_of(p) == g and pp[p]} | {p for p in act["p"] if group_of(p) == g})
            groups.append({"group": g, "plan": plan_g, "actual": round(act_g, 2), "short": round(plan_g - act_g, 2),
                           "positions": [{"position": p, "plan": pp.get(p, 0), "actual": round(act["p"].get(p, 0.0), 2)}
                                         for p in positions]})
        plan_total = sum(g["plan"] for g in groups)
        actual_total = sum(g["actual"] for g in groups)
        missing = 0.0 if no_ts else sum(max(0.0, g["short"]) for g in groups if g["plan"] > 0)
        future = []
        for d in shown:
            fp, fg = plan_for(name, d) if wk(d)[0] in {k[0] for v in plan.values() for k in v} else ({}, {})
            future.append({"week": wk(d)[1], "start": d.isoformat(), "total": sum(fg.values()),
                           "groups": {g: v for g, v in fg.items() if v},
                           "positions": {p: v for p, v in fp.items() if v}})
        yard = site_of(name, names)
        rel = [r for r in requests if (r["yard"] == yard or r["project"] == name)]
        near = sum(r["still"] * (1.0 if r["week"] <= week + 1 else 0.6 if r["week"] <= week + 3 else 0.3)
                   for r in rel if r["still"] > 0)
        pct = actual_total / plan_total if plan_total and not no_ts else 1.0
        repeated = sum(1 for h in hist if h["below"])
        score = round(missing * 3 + (1 - min(pct, 1.0)) * 5 + near * 2 + repeated * 1.5, 2)
        if not plan_total and not any(f["total"] for f in future):
            continue
        staffing.append({"project": name, "yard": yard, "country": country, "last_week": wk(last)[1],
                         "no_ts": no_ts,
                         "plan": plan_total, "actual": None if no_ts else round(actual_total, 2),
                         "pct": round(pct * 100) if plan_total and not no_ts else None,
                         "missing": round(missing, 2),
                         "below": not no_ts and any(g["plan"] > 0 and g["short"] >= SHORTAGE_ALERT for g in groups),
                         "groups": groups, "history": hist, "repeated": repeated, "future": future,
                         "requests": [r["id"] for r in rel], "score": score})
    staffing.sort(key=lambda s: -s["score"])

    # ---- workers: timesheet checks and working-time alerts (anonymous codes only)
    d28, d112 = today - dt.timedelta(days=28), today - dt.timedelta(days=112)
    weeks4 = [wk(this_monday - dt.timedelta(days=7 * i)) for i in range(3, -1, -1)]
    workers = []
    for p in people:
        n = p["name"]
        days = {dt.date.fromisoformat(k): v for k, v in daily.get(n, {}).items()}
        recent = {d: v for d, v in days.items() if d > d28}
        last16 = {d: v for d, v in days.items() if d > d112}
        week_h = defaultdict(float)
        for d, v in last16.items():
            week_h[wk(d)] += v
        monthly = defaultdict(lambda: [0, 0])
        all_weeks = defaultdict(float)
        for d, v in days.items():
            if v > 12 and d.isoformat()[:7] in midx:
                monthly[midx[d.isoformat()[:7]]][0] += 1
            all_weeks[monday(d)] += v
        for mon, v in all_weeks.items():
            m = (mon + dt.timedelta(days=3)).isoformat()[:7]
            if v > 60 and m in midx:
                monthly[midx[m]][1] += 1
        w = {"code": worker_code(p["position"], p["rid"] or n), "rid": p["rid"], "yard": p["yard"],
             "project": p["project"], "country": p["country"], "group": p["group"], "position": p["position"],
             "status": p["status"], "own": p["own"],
             "weeks": [round(week_h.get(k, 0)) for k in weeks4],
             "total": round(total_h.get(n, 0)), "last": max(days).isoformat() if days else "",
             "max_day": round(max(recent.values()), 1) if recent else 0,
             "days_over": sum(1 for v in recent.values() if v > 12),
             "weeks_over": sum(1 for v in week_h.values() if v > 60),
             "avg16": round(sum(last16.values()) / 16, 1),
             "monthly": {str(k): v for k, v in monthly.items()}}
        flags = []
        internal = p["project"] in INTERNAL_TIMESHEETS or p["yard"] in INTERNAL_TIMESHEETS
        if p["status"] == "Yes" and not internal and p["yard"] in yards_with_ts and not last_week_yards.get(n):
            flags.append("no_ts")
        mism = [] if p["yard"] == UNASSIGNED else sorted(y for y in last_week_yards.get(n, ()) if y != p["yard"])
        if mism:
            flags.append("mismatch")
            w["mismatch"] = mism
        if w["days_over"]:
            flags.append("over12")
        if w["weeks_over"]:
            flags.append("over60")
        if w["avg16"] > 48:
            flags.append("avg48")
        w["flags"] = flags
        workers.append(w)
    workers.sort(key=lambda w: (not w["flags"], w["yard"], w["position"], w["code"]))

    # ---- workforce and snapshot
    yards = {}
    for p in people:
        yards.setdefault(p["yard"], p["country"])
    unassigned = Counter(p["group"] for p in people if p["yard"] == UNASSIGNED)
    for k in yard_first:
        yards.setdefault(k, labour.country_of(k))
    status_total = Counter(p["status"] for p in people)
    by_yard_onsite = Counter(p["yard"] for p in people if p["status"] == "Yes")
    lw, pw = wk(completed[-1]), wk(completed[-2])
    return {
        "date": today_s,
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "year": year, "week": week,
        "last_week": {"week": lw[1], "start": last_mon.isoformat(), "end": last_sun.isoformat()},
        "weeks": [{"week": wk(d)[1], "start": d.isoformat()} for d in shown],
        "groups": GROUP_ORDER,
        "position_groups": POSITION_GROUPS,
        "people": [[worker_code(p["position"], p["rid"] or p["name"]), p["rid"], p["yard"], p["project"], p["country"],
                    p["group"], p["position"], p["status"], int(p["own"])] for p in people],
        "yards": yards,
        "workforce": {"total": len(people), "status": dict(status_total),
                      "own": sum(1 for p in people if p["own"]), "sub": sum(1 for p in people if not p["own"]),
                      "by_yard_onsite": dict(by_yard_onsite), "unassigned": sum(unassigned.values()),
                      "unassigned_by_group": dict(unassigned)},
        "unassigned_key": UNASSIGNED,
        "staffing": staffing,
        "requests": requests,
        "hours": {"months": months, "rows": hours_rows, "yard_first": {k: midx[v] for k, v in yard_first.items() if v in midx},
                  "total": round(hours_total), "cost": round(cost_total)},
        "workers": workers,
        "cost_model": {"lt_factor": round(LT_FACTOR, 2), "trip_factor": round(TRIP_FACTOR, 2),
                       "trip_salary_share": TRIP_SALARY_SHARE, "sub_rate": SUBCONTRACTOR_RATE,
                       "hours_by_source": {k: round(v) for k, v in labour.stats.items()},
                       "cost_last_week": round(cost_week.get(lw, 0)), "cost_prev_week": round(cost_week.get(pw, 0))},
        "data": {"latest_timesheet": latest_entry, "timesheet_rows": rows_used,
                 "yards_without_timesheets": dict(Counter(
                     p["yard"] for p in people if p["status"] == "Yes" and p["yard"] not in yards_with_ts
                     and p["yard"] not in INTERNAL_TIMESHEETS and p["project"] not in INTERNAL_TIMESHEETS))},
        "links": {"people": f"{ragic}/planing/19/", "requests": f"{ragic}/planing/52/"},
    }


# ---------- history snapshot (compact) and categorised changes ----------

def compact(snap):
    flag_codes = defaultdict(list)
    for w in snap["workers"]:
        for f in w["flags"]:
            flag_codes[f].append(w["code"])
    return {"date": snap["date"], "generated": snap["generated"], "v": 2, "week": snap["week"],
            "workforce": snap["workforce"],
            "requests": {str(r["id"]): {k: r[k] for k in ("project", "yard", "position", "week", "requested", "still", "status")}
                         for r in snap["requests"]},
            "staffing": {s["project"]: {"missing": s["missing"], "below": s["below"], "yard": s["yard"]} for s in snap["staffing"]},
            "flags": dict(flag_codes),
            "cost": {"last_week": snap["cost_model"]["cost_last_week"]},
            "data": snap["data"]}


def diff(prev, cur):
    """Structured change items; the page renders the text in the active language."""
    if not prev or prev.get("v") != 2:
        return {"since": prev["date"] if prev else None, "items": [], "first": True}
    items = []

    def add(cat, key, imp, mag=0, **p):
        items.append({"cat": cat, "k": key, "imp": int(imp), "mag": abs(mag), "p": p})

    pw, cw = prev["workforce"], cur["workforce"]
    if cw["total"] != pw["total"]:
        add("workforce", "active_total", abs(cw["total"] - pw["total"]) >= 5, cw["total"] - pw["total"], a=pw["total"], b=cw["total"])
    if cw.get("unassigned", 0) != pw.get("unassigned", 0):
        add("workforce", "unassigned", abs(cw.get("unassigned", 0) - pw.get("unassigned", 0)) >= 3,
            cw.get("unassigned", 0) - pw.get("unassigned", 0), a=pw.get("unassigned", 0), b=cw.get("unassigned", 0))
    for st in STATUS_ORDER:
        a, b = pw["status"].get(st, 0), cw["status"].get(st, 0)
        if a != b:
            add("workforce", "status_" + st, abs(b - a) >= 5, b - a, a=a, b=b)
    for yard in sorted((set(pw.get("by_yard_onsite", {})) | set(cw.get("by_yard_onsite", {}))) - {UNASSIGNED}):
        a, b = pw.get("by_yard_onsite", {}).get(yard, 0), cw.get("by_yard_onsite", {}).get(yard, 0)
        if a != b:
            add("locations", "yard_onsite", abs(b - a) >= 3, b - a, yard=yard, a=a, b=b)

    pr, cr = prev["requests"], cur["requests"]
    for rid, r in cr.items():
        o = pr.get(rid)
        if not o:
            add("requests", "req_new", True, r["requested"], yard=r["yard"], project=r["project"], position=r["position"], n=r["requested"], week=r["week"])
            continue
        if o["status"] != r["status"]:
            add("requests", "req_status", True, 1,
                project=r["project"], position=r["position"], a=o["status"], b=r["status"], week=r["week"])
        if o["still"] != r["still"]:
            add("requests", "req_still", abs(r["still"] - o["still"]) >= 2, r["still"] - o["still"],
                project=r["project"], position=r["position"], a=o["still"], b=r["still"])
    for rid, o in pr.items():
        # requests that only aged out of the shown window (older than 4 weeks) are not "removed"
        if rid not in cr and o["week"] >= cur.get("week", 0) - 4:
            add("requests", "req_gone", False, 1, project=o["project"], position=o["position"], week=o["week"])

    ps, cs = prev["staffing"], cur["staffing"]
    for proj, s in cs.items():
        o = ps.get(proj)
        if not o:
            continue
        if s["below"] and not o["below"]:
            add("staffing", "staff_below", s["missing"] >= 1.0, s["missing"], project=proj, missing=s["missing"])
        elif s["missing"] - o["missing"] >= 1.0:
            add("staffing", "staff_worse", True, s["missing"] - o["missing"], project=proj, a=o["missing"], b=s["missing"])
        elif o["below"] and not s["below"]:
            add("staffing", "staff_ok", False, 1, project=proj)

    pf, cf = prev["flags"], cur["flags"]
    for f, cat in (("mismatch", "timesheets"), ("no_ts", "timesheets"), ("over12", "timesheets"), ("over60", "timesheets"), ("avg48", "timesheets")):
        new = set(cf.get(f, [])) - set(pf.get(f, []))
        gone = set(pf.get(f, [])) - set(cf.get(f, []))
        if new:
            add(cat, "flag_new_" + f, True, len(new), n=len(new))
        if gone:
            add(cat, "flag_gone_" + f, False, len(gone), n=len(gone))

    a, b = prev["data"].get("latest_timesheet", ""), cur["data"].get("latest_timesheet", "")
    if b and a == b:
        add("data", "ts_not_updated", True, 1, date=b)
    ca, cb = prev["cost"].get("last_week", 0), cur["cost"].get("last_week", 0)
    if ca and cb and abs(cb - ca) / ca >= 0.15:
        add("cost", "cost_pace", True, (cb - ca) / ca * 100, a=ca, b=cb)
    items.sort(key=lambda i: (-i["imp"], -i["mag"]))
    return {"since": prev["date"], "items": items, "first": False}


def trend(history, cur):
    series = {}
    for s in history + [cur]:
        st = s["workforce"]["status"]
        series[s["date"]] = {k: st.get(k, 0) for k in STATUS_ORDER}
    return [{"date": d, **v} for d, v in sorted(series.items())][-180:]


def main():
    raw_dir, hist_dir, out_html = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    raw = {n: json.loads((raw_dir / f"{n}.json").read_text(encoding="utf-8"))
           for n in ("projects", "people", "requests", "hours", "qualification", "companies", "rotation")
           if (raw_dir / f"{n}.json").exists()}
    today = dt.date.fromisoformat(os.environ["BUILD_DATE"]) if os.environ.get("BUILD_DATE") else dt.date.today()
    snap = build_snapshot(raw, today)
    small = compact(snap)
    history = [h for h in load_history(hist_dir) if h["date"] < snap["date"]]
    prev = history[-1] if history else None
    save_snapshot(hist_dir, small)

    page = dict(snap)
    page["changes"] = diff(prev, small)
    page["trend"] = trend([h for h in history if h.get("v") == 2], small)
    page["mgmt_hash"] = hashlib.sha256((os.environ.get("MGMT_PASSWORD") or "management").encode()).hexdigest()
    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(page, ensure_ascii=False).replace("</", "<\\/")
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(template.replace("/*__DATA__*/null", payload), encoding="utf-8")
    print(f"built {out_html} for {snap['date']} (W{snap['week']}); previous snapshot: {prev['date'] if prev else 'none'}")


if __name__ == "__main__":
    main()
