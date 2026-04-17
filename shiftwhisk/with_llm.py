"""
Restaurant shift scheduling system with natural language input.

Setup:
    pip install ortools requests flask flask-cors

    Groq API key: https://console.groq.com/home

Run standalone CLI:
    GROQ_API_KEY=your-key python3 with_llm.py --data restaurant_data.json

Run as part of the ShiftWhisk web app:
    Start app.py instead — this module is imported by the Flask backend.

Scheduling priorities (in order):
    1. Hard constraints  — availability, skills, staffing counts, max hours
    2. Full-time first   — employees with max_hours >= 30 get shifts filled first
    3. Seniority         — on busy shifts (evening / weekend), prefer senior staff
    4. Fairness          — after full-time staff are satisfied, distribute evenly
    5. Stability         — minimise unnecessary changes from the previous schedule
    6. User preferences  — avoid_shift, avoid_back_to_back, shift_preference
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model
import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_PREFERENCE_PENALTY   = -10
MAX_PREFERENCE_PENALTY   = 10
FULLTIME_HOURS_THRESHOLD = 30  # max_hours >= this → treated as full-time

DAY_ALIASES: Dict[str, str] = {
    "m": "Monday", "mon": "Monday", "monday": "Monday",
    "tu": "Tuesday", "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday",
    "w": "Wednesday", "wed": "Wednesday", "weds": "Wednesday", "wednesday": "Wednesday",
    "th": "Thursday", "thu": "Thursday", "thur": "Thursday", "thurs": "Thursday", "thursday": "Thursday",
    "f": "Friday", "fri": "Friday", "friday": "Friday",
    "sa": "Saturday", "sat": "Saturday", "saturday": "Saturday",
    "su": "Sunday", "sun": "Sunday", "sunday": "Sunday",
}

SHIFT_ALIASES: Dict[str, str] = {
    "morning": "morning", "am": "morning", "a m": "morning",
    "a.m": "morning", "a.m.": "morning", "day": "morning",
    "dayshift": "morning", "day shift": "morning",
    "evening": "evening", "pm": "evening", "p m": "evening",
    "p.m": "evening", "p.m.": "evening", "night": "evening",
    "nightshift": "evening", "night shift": "evening",
}

YES_ALIASES = {"y", "yes", "yeah", "yep", "sure", "ok", "okay", "true", "1"}
NO_ALIASES  = {"n", "no", "nope", "false", "0"}

ORDERED_DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def simplify_text(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"[.]+", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def resolve_with_aliases(raw_value: str, aliases: Dict[str, str],
                          label: str, examples: str, cutoff: float = 0.72) -> str:
    normalized = simplify_text(raw_value)
    compact    = normalized.replace(" ", "")
    if normalized in aliases: return aliases[normalized]
    if compact    in aliases: return aliases[compact]
    candidates = list(dict.fromkeys(list(aliases.keys()) + [k.replace(" ","") for k in aliases.keys()]))
    match = difflib.get_close_matches(normalized, candidates, n=1, cutoff=cutoff)
    if not match:
        match = difflib.get_close_matches(compact, candidates, n=1, cutoff=cutoff)
    if match:
        mk = match[0]
        if mk in aliases: return aliases[mk]
        for k, v in aliases.items():
            if k.replace(" ", "") == mk: return v
    raise ValueError(f"Invalid {label} '{raw_value}'. Examples: {examples}.")


def normalize_day(day: str) -> str:
    return resolve_with_aliases(day, DAY_ALIASES, "day",
                                "Mon, Tues, Wednesday, Thu, Fri, Sat, Sun")

def normalize_shift(shift: str) -> str:
    """Normalise a shift string using SHIFT_ALIASES with fuzzy matching.

    If the shift does not match any alias (e.g. custom UI name like 'Afternoon'),
    return it lowercased instead of raising — custom shift names must pass through
    unchanged so the availability lookup works correctly.
    """
    try:
        return resolve_with_aliases(shift, SHIFT_ALIASES, "shift",
                                    "AM, morning, day shift, PM, evening, night shift")
    except ValueError:
        # Not a standard alias — custom UI shift name, preserve lowercased
        return simplify_text(shift)

def normalize_optional_day(day: Optional[str]) -> Optional[str]:
    return normalize_day(day) if day else None

def normalize_optional_shift(shift: Optional[str]) -> Optional[str]:
    return normalize_shift(shift) if shift else None

def clamp_preference_penalty(value: int) -> int:
    if not (MIN_PREFERENCE_PENALTY <= value <= MAX_PREFERENCE_PENALTY):
        raise ValueError(f"Penalty must be between {MIN_PREFERENCE_PENALTY} and {MAX_PREFERENCE_PENALTY}.")
    return value


# ---------------------------------------------------------------------------
# Employee name resolution with fuzzy matching
# ---------------------------------------------------------------------------

def resolve_employee_name_fuzzy(staff: List[Dict[str, Any]], employee_name: str) -> Dict[str, Any]:
    normalized  = simplify_text(employee_name)
    name_lookup = {simplify_text(s["name"]): s for s in staff}
    if normalized in name_lookup:
        return name_lookup[normalized]
    matches = difflib.get_close_matches(normalized, list(name_lookup.keys()), n=1, cutoff=0.72)
    if matches:
        return name_lookup[matches[0]]
    available = ", ".join(s["name"] for s in staff)
    raise ValueError(f"Employee '{employee_name}' not found. Available: {available}.")


# ---------------------------------------------------------------------------
# Data loading & validation
# ---------------------------------------------------------------------------

def load_data_from_json(file_path: str) -> Dict[str, Any]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {file_path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    validate_data(data)
    return data

def save_data_to_json(data: Dict[str, Any], file_path: str) -> None:
    with Path(file_path).open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def validate_data(data: Dict[str, Any]) -> None:
    required = {"staff", "availability", "shift_requirements"}
    missing  = required - set(data.keys())
    if missing: raise ValueError(f"Missing top-level keys: {missing}")

    for s in data["staff"]:
        for key in ["id", "name", "skills", "max_hours"]:
            if key not in s: raise ValueError(f"Staff record missing '{key}': {s}")

    for row in data["availability"]:
        for key in ["employee_id", "day", "shift", "available"]:
            if key not in row: raise ValueError(f"Availability record missing '{key}': {row}")
        row["day"]   = normalize_day(row["day"])
        row["shift"] = normalize_shift(row["shift"])

    for req in data["shift_requirements"]:
        for key in ["day", "shift", "role", "required_count"]:
            if key not in req: raise ValueError(f"Shift requirement missing '{key}': {req}")
        req["day"]   = normalize_day(req["day"])
        req["shift"] = normalize_shift(req["shift"])

    for pref in data.get("preferences", []):
        if "type" not in pref or "employee_name" not in pref:
            raise ValueError(f"Preference missing required fields: {pref}")
        if "day"   in pref: pref["day"]   = normalize_day(pref["day"])
        if "shift" in pref: pref["shift"] = normalize_shift(pref["shift"])
        if pref["type"] in {"shift_preference","avoid_shift","back_to_back_preference","avoid_back_to_back"}:
            clamp_preference_penalty(int(pref.get("penalty", 5)))


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def make_availability_lookup(availability: List[Dict[str, Any]]) -> Dict[Tuple, bool]:
    return {
        (row["employee_id"], row["day"].lower(), row["shift"].lower()): bool(row["available"])
        for row in availability
    }

def get_staff_lookup(staff: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {s["id"]: s for s in staff}

def schedule_to_assignment_set(schedule: List[Dict[str, str]]) -> set:
    return {(r["employee_id"], r["day"], r["shift"], r["role"]) for r in schedule}

def is_busy_shift(day: str, shift: str) -> bool:
    try:    norm_shift = normalize_shift(shift)
    except: norm_shift = shift.lower()
    try:    norm_day   = normalize_day(day)
    except: norm_day   = day
    return norm_shift == "evening" or norm_day in {"Saturday", "Sunday"}

def is_fulltime(emp: Dict[str, Any]) -> bool:
    return float(emp.get("max_hours", 0)) >= FULLTIME_HOURS_THRESHOLD

def build_assignment_flag(model, vars_list, name):
    flag = model.NewBoolVar(name)
    model.Add(sum(vars_list) >= 1).OnlyEnforceIf(flag)
    model.Add(sum(vars_list) == 0).OnlyEnforceIf(flag.Not())
    return flag

def get_staff_names(data: Dict[str, Any]) -> List[str]:
    return [s["name"] for s in data["staff"]]

def get_sorted_days(days: List[str]) -> List[str]:
    order = {d: i for i, d in enumerate(ORDERED_DAYS)}
    return sorted(set(days), key=lambda d: order.get(d, 999))


# ---------------------------------------------------------------------------
# Availability mutations
# ---------------------------------------------------------------------------

def set_availability(data, employee_id, day, shift, available):
    norm_day   = normalize_day(day)
    norm_shift = normalize_shift(shift)
    found = False
    for row in data["availability"]:
        if (row["employee_id"] == employee_id
                and row["day"].lower()   == norm_day.lower()
                and row["shift"].lower() == norm_shift.lower()):
            row["available"] = available
            row["day"]       = norm_day
            row["shift"]     = norm_shift
            found = True
    if not found:
        data["availability"].append({
            "employee_id": employee_id, "day": norm_day,
            "shift": norm_shift, "available": available,
        })

def set_availability_by_pattern(data, employee_id, available,
                                 target_day=None, target_shift=None):
    norm_day   = normalize_optional_day(target_day)
    norm_shift = normalize_optional_shift(target_shift)
    if norm_day is None and norm_shift is None:
        raise ValueError("At least one of target_day or target_shift must be provided.")

    all_pairs: set = set()
    for row in data["availability"]:
        if row["employee_id"] == employee_id:
            all_pairs.add((row["day"], row["shift"]))
    for req in data["shift_requirements"]:
        all_pairs.add((req["day"], req["shift"]))

    matched = [
        (d, s) for d, s in sorted(all_pairs)
        if (norm_day   is None or d.lower() == norm_day.lower())
        and (norm_shift is None or s.lower() == norm_shift.lower())
    ]
    if not matched:
        raise ValueError(
            f"No matching slots for employee_id={employee_id}, "
            f"day={norm_day}, shift={norm_shift}."
        )
    for d, s in matched:
        set_availability(data, employee_id, d, s, available)

def add_preference(data, preference):
    if "day"   in preference: preference["day"]   = normalize_day(preference["day"])
    if "shift" in preference: preference["shift"] = normalize_shift(preference["shift"])
    data.setdefault("preferences", []).append(preference)


# ---------------------------------------------------------------------------
# Schedule query helpers
# ---------------------------------------------------------------------------

def get_assignments_for_employee_shift(schedule, employee_name, day, shift):
    norm_name  = simplify_text(employee_name)
    norm_day   = normalize_day(day)
    norm_shift = normalize_shift(shift)
    return [
        r for r in schedule
        if simplify_text(r["employee_name"]) == norm_name
        and r["day"].lower()   == norm_day.lower()
        and r["shift"].lower() == norm_shift.lower()
    ]

def get_employee_assignment_roles(schedule, employee_name, day, shift):
    return [r["role"] for r in get_assignments_for_employee_shift(schedule, employee_name, day, shift)]

def get_removed_and_added_assignments(old_schedule, new_schedule):
    def to_set(s):
        return {(r["employee_id"],r["employee_name"],r["day"],r["shift"],r["role"]) for r in s}
    removed = to_set(old_schedule) - to_set(new_schedule)
    added   = to_set(new_schedule) - to_set(old_schedule)
    def to_rows(s):
        return [{"employee_id":a,"employee_name":b,"day":c,"shift":d,"role":e}
                for a,b,c,d,e in sorted(s)]
    return to_rows(removed), to_rows(added)


# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

def solve_schedule(data, preferred_schedule=None, forced_assignments=None):
    """
    Scheduling priorities:
      1. Hard: availability, skills, staffing counts, max-hours cap
      2. Full-time first (max_hours >= 30): penalise part-timers filling slots
      3. Seniority on busy shifts: penalise low-seniority on evening/weekend
      4. Fairness: minimise max load across part-time employees
      5. Senior coverage: soft penalty when a shift has no senior (seniority>=2)
         if a senior was available — ensures each slot has at least 1 experienced person
      6. Stability: reward keeping previous assignments
      7. User preferences: shift_preference / avoid_back_to_back
    """
    staff        = data["staff"]
    availability = data["availability"]
    requirements = data["shift_requirements"]
    preferences  = data.get("preferences", [])

    staff_lookup          = get_staff_lookup(staff)
    name_to_id            = {simplify_text(s["name"]): s["id"] for s in staff}
    avail_lookup          = make_availability_lookup(availability)
    preferred_assignments = schedule_to_assignment_set(preferred_schedule or [])
    forced_assignments    = forced_assignments or []

    model = cp_model.CpModel()
    x: Dict[Tuple, Any] = {}

    all_roles           = sorted({r["role"]  for r in requirements})
    all_day_shift_pairs = sorted({(r["day"], r["shift"]) for r in requirements})
    all_days            = get_sorted_days([r["day"] for r in requirements])
    all_shifts          = sorted({r["shift"] for r in requirements})

    # Hard 1: create vars, lock impossible assignments
    for req in requirements:
        day, shift, role = req["day"], req["shift"], req["role"]
        for emp in staff:
            emp_id       = emp["id"]
            has_skill    = role.lower() in {s.lower() for s in emp["skills"]}
            is_available = avail_lookup.get((emp_id, day.lower(), shift.lower()), False)
            var = model.NewBoolVar(f"x_{emp_id}_{day}_{shift}_{role}")
            x[(emp_id, day, shift, role)] = var
            if not has_skill or not is_available:
                model.Add(var == 0)

    # Hard 2: exact staffing counts
    for req in requirements:
        day, shift, role = req["day"], req["shift"], req["role"]
        model.Add(sum(x[(emp["id"], day, shift, role)] for emp in staff) == int(req["required_count"]))

    # Hard 3: one role per shift per employee
    for emp in staff:
        emp_id = emp["id"]
        for day, shift in all_day_shift_pairs:
            svars = [x[(emp_id,day,shift,r)] for r in all_roles if (emp_id,day,shift,r) in x]
            if svars: model.Add(sum(svars) <= 1)

    # Hard 4: max hours cap
    max_possible = len(all_day_shift_pairs)
    total_assigned_per_emp: Dict[str, Any] = {}
    for emp in staff:
        emp_id      = emp["id"]
        emp_vars    = [var for key, var in x.items() if key[0] == emp_id]
        shift_hours = float(emp.get("shift_duration_hours", 8.0))
        max_hrs     = float(emp.get("max_hours", 40.0))
        max_shifts_emp = max(1, int(max_hrs / shift_hours)) if shift_hours > 0 else 5
        model.Add(sum(emp_vars) <= max_shifts_emp)
        total_var = model.NewIntVar(0, max_possible, f"total_{emp_id}")
        model.Add(total_var == sum(emp_vars))
        total_assigned_per_emp[emp_id] = total_var

    # Forced assignments (e.g. direct swap)
    for assignment in forced_assignments:
        if assignment not in x:
            raise ValueError(f"Forced assignment not in model: {assignment}")
        model.Add(x[assignment] == 1)

    # Stability
    keep_vars = (
        [var for key, var in x.items() if key in preferred_assignments]
        if preferred_schedule else []
    )

    penalty_terms: List = []

    # Priority 2 — Full-time first
    # Part-timers filling a shift slot gets a penalty so solver prefers full-timers.
    FULLTIME_PRIORITY_WEIGHT = 6
    for (emp_id, day, shift, role), var in x.items():
        if not is_fulltime(staff_lookup[emp_id]):
            penalty_terms.append(var * FULLTIME_PRIORITY_WEIGHT)

    # Priority 3 — Seniority on busy shifts
    # seniority 0 → weight 4, seniority 1 → weight 2, seniority >= 2 → 0
    for (emp_id, day, shift, role), var in x.items():
        seniority = int(staff_lookup[emp_id].get("seniority", 0))
        if is_busy_shift(day, shift) and seniority < 2:
            penalty_terms.append(var * max(1, 4 - seniority * 2))

    # Priority 4 — Fairness among part-timers
    pt_totals = [total_assigned_per_emp[emp["id"]] for emp in staff if not is_fulltime(emp)]
    max_pt_load = model.NewIntVar(0, max_possible, "max_pt_load")
    if pt_totals:
        for tv in pt_totals: model.Add(tv <= max_pt_load)

    # Priority 5 — Senior coverage: each shift should have at least 1 senior
    # (seniority >= 2) if one is available and eligible for that slot.
    # This is a soft constraint — we penalise shifts with no senior assigned
    # only when a senior could have been assigned.
    SENIOR_COVERAGE_WEIGHT = 3   # keep lower than full-time weight (6) so
                                  # full-time priority still dominates
    SENIOR_THRESHOLD = 2

    for day, shift in all_day_shift_pairs:
        # Collect all assignment vars for senior employees in this slot
        senior_vars = [
            x[(emp["id"], day, shift, role)]
            for emp in staff
            for role in all_roles
            if (emp["id"], day, shift, role) in x
            and int(emp.get("seniority", 0)) >= SENIOR_THRESHOLD
        ]
        # Only add penalty when at least one senior is eligible for this slot
        if not senior_vars:
            continue

        # no_senior = 1 when no senior is assigned to this slot
        no_senior = model.NewBoolVar(f"no_senior_{day}_{shift}")
        senior_flag = build_assignment_flag(
            model, senior_vars, f"senior_flag_{day}_{shift}"
        )
        # no_senior is the logical NOT of senior_flag
        model.Add(no_senior == 1).OnlyEnforceIf(senior_flag.Not())
        model.Add(no_senior == 0).OnlyEnforceIf(senior_flag)
        penalty_terms.append(no_senior * SENIOR_COVERAGE_WEIGHT)

    # Priority 6 — User preferences
    for i, pref in enumerate(preferences):
        pref_type    = pref["type"]
        emp_name_key = simplify_text(pref["employee_name"])
        if emp_name_key not in name_to_id: continue
        emp_id  = name_to_id[emp_name_key]
        penalty = clamp_preference_penalty(int(pref.get("penalty", 5)))

        if pref_type in {"shift_preference", "avoid_shift"}:
            target_day   = pref.get("day")
            target_shift = pref.get("shift")
            for role in all_roles:
                for day in all_days:
                    for shift in all_shifts:
                        dm = (target_day   is None or day.lower()   == target_day.lower())
                        sm = (target_shift is None or shift.lower() == target_shift.lower())
                        if dm and sm:
                            key = (emp_id, day, shift, role)
                            if key in x: penalty_terms.append(x[key] * penalty)

        elif pref_type in {"back_to_back_preference", "avoid_back_to_back"}:
            # Same-day morning + evening
            for day in all_days:
                m_vars = [x[(emp_id,day,s,r)] for s in all_shifts for r in all_roles
                          if "morning" in s.lower() and (emp_id,day,s,r) in x]
                e_vars = [x[(emp_id,day,s,r)] for s in all_shifts for r in all_roles
                          if "evening" in s.lower() and (emp_id,day,s,r) in x]
                if not m_vars or not e_vars: continue
                mf  = build_assignment_flag(model, m_vars, f"m_{emp_id}_{day}_{i}")
                ef  = build_assignment_flag(model, e_vars, f"e_{emp_id}_{day}_{i}")
                b2b = model.NewBoolVar(f"b2b_{emp_id}_{day}_{i}")
                model.AddBoolAnd([mf, ef]).OnlyEnforceIf(b2b)
                model.AddBoolOr([mf.Not(), ef.Not()]).OnlyEnforceIf(b2b.Not())
                penalty_terms.append(b2b * penalty)
            # Cross-day evening → next morning
            for di in range(len(all_days) - 1):
                day, next_day = all_days[di], all_days[di + 1]
                e_vars  = [x[(emp_id,day,s,r)]      for s in all_shifts for r in all_roles
                           if "evening" in s.lower()  and (emp_id,day,s,r)      in x]
                nm_vars = [x[(emp_id,next_day,s,r)]  for s in all_shifts for r in all_roles
                           if "morning" in s.lower()  and (emp_id,next_day,s,r) in x]
                if not e_vars or not nm_vars: continue
                ef  = build_assignment_flag(model, e_vars,  f"ce_{emp_id}_{day}_{i}")
                nmf = build_assignment_flag(model, nm_vars, f"cnm_{emp_id}_{next_day}_{i}")
                cross = model.NewBoolVar(f"cross_{emp_id}_{day}_{i}")
                model.AddBoolAnd([ef, nmf]).OnlyEnforceIf(cross)
                model.AddBoolOr([ef.Not(), nmf.Not()]).OnlyEnforceIf(cross.Not())
                penalty_terms.append(cross * penalty)

    # Objective
    total_penalty = model.NewIntVar(0, 500_000, "total_penalty")
    model.Add(total_penalty == (sum(penalty_terms) if penalty_terms else 0))

    if keep_vars:
        model.Maximize(sum(keep_vars) * 1000 - total_penalty * 10 - max_pt_load)
    else:
        model.Minimize(total_penalty * 10 + max_pt_load)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 15
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ValueError("No feasible schedule found under current constraints.")

    schedule = [
        {"employee_id": emp_id, "employee_name": staff_lookup[emp_id]["name"],
         "day": day, "shift": shift, "role": role}
        for (emp_id, day, shift, role), var in x.items()
        if solver.Value(var) == 1
    ]
    schedule.sort(key=lambda r: (r["day"], r["shift"], r["role"], r["employee_name"]))
    return schedule


def generate_schedule(data):
    return solve_schedule(data, preferred_schedule=None)


# ---------------------------------------------------------------------------
# Update schedule
# ---------------------------------------------------------------------------

def update_schedule(data, existing_schedule, change):
    change_type  = change.get("type", "").strip()
    updated_data = {
        "staff":              [dict(s) for s in data["staff"]],
        "availability":       [dict(a) for a in data["availability"]],
        "shift_requirements": [dict(r) for r in data["shift_requirements"]],
        "preferences":        [dict(p) for p in data.get("preferences", [])],
    }

    if change_type == "unavailable":
        employee_name = change["employee_name"].strip()
        day           = change["day"].strip()
        shift         = change.get("shift", "").strip()
        emp    = resolve_employee_name_fuzzy(data["staff"], employee_name)
        emp_id = emp["id"]

        if shift.lower() == "all" or not shift:
            set_availability_by_pattern(updated_data, emp_id, False, target_day=day)
        else:
            try:
                norm_shift = normalize_shift(shift)
            except ValueError:
                known = sorted({r["shift"] for r in data["shift_requirements"]})
                norm_shift = next(
                    (s for s in known if shift.lower() in s.lower() or s.lower() in shift.lower()),
                    shift
                )
            set_availability(updated_data, emp_id, day, norm_shift, False)
        return solve_schedule(updated_data, preferred_schedule=existing_schedule), updated_data

    if change_type == "direct_swap":
        name_1, day_1, shift_1 = change["employee_name_1"].strip(), change["day_1"].strip(), change["shift_1"].strip()
        name_2, day_2, shift_2 = change["employee_name_2"].strip(), change["day_2"].strip(), change["shift_2"].strip()
        emp_1 = resolve_employee_name_fuzzy(data["staff"], name_1)
        emp_2 = resolve_employee_name_fuzzy(data["staff"], name_2)
        if emp_1["id"] == emp_2["id"]:
            raise ValueError("direct_swap requires two different employees.")
        roles_1 = get_employee_assignment_roles(existing_schedule, name_1, day_1, shift_1)
        roles_2 = get_employee_assignment_roles(existing_schedule, name_2, day_2, shift_2)
        if not roles_1: raise ValueError(f"{name_1} is not assigned to {day_1} {shift_1}.")
        if not roles_2: raise ValueError(f"{name_2} is not assigned to {day_2} {shift_2}.")
        if len(roles_1) != 1 or len(roles_2) != 1:
            raise ValueError("Each employee must hold exactly one role in the specified shift.")
        role_1, role_2 = roles_1[0], roles_2[0]
        if role_2.lower() not in {s.lower() for s in emp_1["skills"]}:
            raise ValueError(f"{name_1} lacks skill '{role_2}' for the swap.")
        if role_1.lower() not in {s.lower() for s in emp_2["skills"]}:
            raise ValueError(f"{name_2} lacks skill '{role_1}' for the swap.")
        set_availability(updated_data, emp_1["id"], day_2, shift_2, True)
        set_availability(updated_data, emp_2["id"], day_1, shift_1, True)
        forced = [
            (emp_1["id"], normalize_day(day_2), normalize_shift(shift_2), role_2),
            (emp_2["id"], normalize_day(day_1), normalize_shift(shift_1), role_1),
        ]
        return solve_schedule(updated_data, preferred_schedule=existing_schedule, forced_assignments=forced), updated_data

    if change_type in {"avoid_back_to_back", "back_to_back_preference"}:
        emp     = resolve_employee_name_fuzzy(data["staff"], change["employee_name"].strip())
        penalty = clamp_preference_penalty(int(change.get("penalty", 5)))
        add_preference(updated_data, {"type":"avoid_back_to_back","employee_name":emp["name"],"penalty":penalty})
        return solve_schedule(updated_data, preferred_schedule=existing_schedule), updated_data

    if change_type in {"avoid_shift", "shift_preference"}:
        emp     = resolve_employee_name_fuzzy(data["staff"], change["employee_name"].strip())
        penalty = clamp_preference_penalty(int(change.get("penalty", 3)))
        pref: Dict[str, Any] = {"type":"shift_preference","employee_name":emp["name"],"penalty":penalty}
        if change.get("day"):   pref["day"]   = normalize_day(change["day"].strip())
        if change.get("shift"): pref["shift"] = normalize_shift(change["shift"].strip())
        add_preference(updated_data, pref)
        return solve_schedule(updated_data, preferred_schedule=existing_schedule), updated_data

    raise ValueError(f"Unknown change type: '{change_type}'")


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------

def generate_explanation(data_before, old_schedule, new_schedule, change):
    change_type  = change.get("type", "").strip()
    removed_rows, added_rows = get_removed_and_added_assignments(old_schedule, new_schedule)
    lines: List[str] = []

    if change_type == "unavailable":
        employee_name = change["employee_name"]
        day           = change["day"]
        shift         = change.get("shift", "all")
        scope = f"{day} (all shifts)" if shift.lower() == "all" else f"{day} {shift}"
        lines.append(f"{employee_name} was marked unavailable for {scope}.")
        replacements = [r for r in added_rows
                        if r["day"].lower() == day.lower()
                        and (shift.lower() == "all" or r["shift"].lower() == shift.lower())]
        for row in replacements:
            emp_info = next((s for s in data_before["staff"]
                             if s["name"].lower() == row["employee_name"].lower()), None)
            senior = emp_info and int(emp_info.get("seniority", 0)) >= 2
            ft     = emp_info and is_fulltime(emp_info)
            reason = "senior and full-time" if senior and ft else ("full-time" if ft else ("senior" if senior else "available"))
            lines.append(f"{row['employee_name']} was assigned as {row['role']} ({reason}).")
        if not replacements:
            lines.append("The schedule was re-optimised with the remaining available staff.")

    elif change_type == "direct_swap":
        lines.append(f"Swapped {change['employee_name_1']} ({change['day_1']} {change['shift_1']}) "
                     f"↔ {change['employee_name_2']} ({change['day_2']} {change['shift_2']}).")
        lines.append("Both employees were qualified for the exchanged roles.")

    elif change_type in {"avoid_back_to_back","back_to_back_preference"}:
        employee_name = change["employee_name"]
        before = after = 0
        for day in {r["day"] for r in old_schedule + new_schedule}:
            def hs(sched, name, d, t):
                return any(simplify_text(r["employee_name"]) == simplify_text(name)
                           and r["day"] == d
                           and ("morning" in r["shift"].lower() if t=="m" else "evening" in r["shift"].lower())
                           for r in sched)
            if hs(old_schedule, employee_name, day, "m") and hs(old_schedule, employee_name, day, "e"): before += 1
            if hs(new_schedule, employee_name, day, "m") and hs(new_schedule, employee_name, day, "e"): after  += 1
        lines.append(f"Preference added: avoid back-to-back shifts for {employee_name}.")
        lines.append(f"Same-day double shifts changed from {before} to {after}.")

    elif change_type in {"avoid_shift","shift_preference"}:
        employee_name = change["employee_name"]
        target_day    = change.get("day")
        target_shift  = change.get("shift")
        penalty       = int(change.get("penalty", 3))
        direction     = "avoid" if penalty > 0 else "prefer"
        def match_target(row):
            if simplify_text(row["employee_name"]) != simplify_text(employee_name): return False
            if target_day   and row["day"].lower()   != target_day.lower():   return False
            if target_shift and row["shift"].lower() != target_shift.lower(): return False
            return True
        scope = " ".join(filter(None, [target_day, target_shift])) or "all shifts"
        before = sum(1 for r in old_schedule if match_target(r))
        after  = sum(1 for r in new_schedule if match_target(r))
        lines.append(f"Preference added: {direction} assigning {employee_name} to {scope}.")
        lines.append(f"Matching assignments changed from {before} to {after}.")
    else:
        lines.append("The schedule was updated and re-optimised.")

    busy_added = [r for r in added_rows if is_busy_shift(r["day"], r["shift"])]
    if busy_added:
        lines.append("Busy shifts prefer full-time and senior staff; "
                     "availability and skill constraints always take priority.")
    if removed_rows or added_rows:
        lines.append("Other assignments were kept unchanged where possible.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM — Groq backend
# ---------------------------------------------------------------------------

_GROQ_MODEL   = "llama-3.3-70b-versatile"
_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM_PROMPT_TEMPLATE = """You are a scheduling assistant. Convert the user's natural language request into a JSON object for a restaurant scheduling system.

STAFF — each entry shows  NAME (ID) [full-time|part-time, seniority]:
{staff_info}

VALID DAYS: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
  Abbreviations fine: mon, tue, wed, thu, fri, sat, sun

VALID SHIFT NAMES: {shift_names}
  Aliases: morning/am/day shift → morning; evening/pm/night → evening

IDENTIFYING AN EMPLOYEE — three options:
1. "employee_id": "XXXXXXXX"   — if user quoted an 8-char ID
2. "employee_name": "NAME"     — if user said a name (typos ok, e.g. Iann→Ian)
   If name is ambiguous: return {{"error":"Ambiguous name — please use the employee ID"}}
3. "slot_lookup": {{"day":"DAY","shift":"SHIFT","role":"ROLE"}}
   — only when user identified someone by their current slot

SHIFT RULES:
- Named shift → use closest match from VALID SHIFT NAMES
- Day mentioned but no shift → set "shift":"all" (unavailable whole day)

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no explanation.

Supported types:

1. unavailable
   {{"type":"unavailable","employee_name":"NAME","day":"DAY","shift":"SHIFT or all"}}

2. direct_swap
   {{"type":"direct_swap","employee_name_1":"N1","day_1":"D","shift_1":"S","employee_name_2":"N2","day_2":"D","shift_2":"S"}}

3. avoid_back_to_back  (penalty 1–10)
   {{"type":"avoid_back_to_back","employee_name":"NAME","penalty":5}}

4. shift_preference  (penalty -10 to 10; omit day or shift if not specified)
   positive=avoid, negative=prefer
   {{"type":"shift_preference","employee_name":"NAME","day":"DAY","shift":"SHIFT","penalty":3}}

EXAMPLES:
User: "Julia cannot work on Monday"
JSON: {{"type":"unavailable","employee_name":"Julia","day":"Monday","shift":"all"}}

User: "Alice can't work Monday morning"
JSON: {{"type":"unavailable","employee_name":"Alice","day":"Monday","shift":"morning"}}

User: "A3F9B2C1 can't come Sunday"
JSON: {{"type":"unavailable","employee_id":"A3F9B2C1","day":"Sunday","shift":"all"}}

User: "Iann can't come Sunday evening"
JSON: {{"type":"unavailable","employee_name":"Iann","day":"Sunday","shift":"evening"}}

User: "swap Cindy on Monday morning with Ian on Tuesday evening"
JSON: {{"type":"direct_swap","employee_name_1":"Cindy","day_1":"Monday","shift_1":"morning","employee_name_2":"Ian","day_2":"Tuesday","shift_2":"evening"}}

User: "Avoid giving Eric back-to-back shifts"
JSON: {{"type":"avoid_back_to_back","employee_name":"Eric","penalty":5}}

User: "Try not to assign Alice to morning shifts"
JSON: {{"type":"shift_preference","employee_name":"Alice","shift":"morning","penalty":3}}

User: "Prefer Brian for Saturday evening"
JSON: {{"type":"shift_preference","employee_name":"Brian","day":"Saturday","shift":"evening","penalty":-4}}

If unclear or missing info: {{"error":"REASON"}}"""


def _call_groq(system_prompt, user_message):
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set. Get one at https://console.groq.com")
    resp = requests.post(
        _GROQ_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": _GROQ_MODEL,
              "messages": [{"role":"system","content":system_prompt},
                           {"role":"user","content":user_message}],
              "temperature": 0, "max_tokens": 512},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def get_staff_info(data):
    lines = []
    for s in data["staff"]:
        ft  = "full-time" if is_fulltime(s) else "part-time"
        sen = int(s.get("seniority", 0))
        lines.append(f"{s['name']} ({s['id']}) [{ft}, {sen} yr seniority]")
    return "\n".join(lines)

def get_shift_names(data):
    names = sorted({r["shift"] for r in data.get("shift_requirements", [])})
    return ", ".join(names) if names else "morning, evening"


def resolve_employee(change, data, current_schedule, field_prefix=""):
    pfx = field_prefix

    emp_id_val = change.get("employee_id") or change.get(f"employee_id{pfx}")
    if emp_id_val:
        emp_id_val = emp_id_val.strip().upper()
        match = next((s for s in data["staff"] if s["id"].upper() == emp_id_val), None)
        if not match: raise ValueError(f"No employee found with ID '{emp_id_val}'.")
        return match["name"]

    name_val = change.get("employee_name") or change.get(f"employee_name{pfx}")
    if name_val:
        return resolve_employee_name_fuzzy(data["staff"], name_val)["name"]

    slot = change.get("slot_lookup") or change.get(f"slot_lookup{pfx}")
    if slot:
        day, shift, role = slot.get("day",""), slot.get("shift",""), slot.get("role","")
        hits = [r for r in current_schedule
                if r["day"].lower()==day.lower()
                and r["shift"].lower()==shift.lower()
                and r["role"].lower()==role.lower()]
        if not hits:
            raise ValueError(f"No one is assigned as {role} on {day} {shift}.")
        if len(hits) > 1:
            names = ", ".join(r["employee_name"] for r in hits)
            raise ValueError(f"Multiple employees in that slot ({names}). Use name or ID.")
        return hits[0]["employee_name"]

    raise ValueError("Could not identify the employee. Provide a name, ID, or shift+role reference.")


def parse_user_request(user_input, data, current_schedule):
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        staff_info  = get_staff_info(data),
        shift_names = get_shift_names(data),
    )
    raw = _call_groq(system_prompt, user_input).strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$",        "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON:\n{raw}\nError: {e}")

    if "error" in result:
        raise ValueError(f"LLM could not parse request: {result['error']}")

    change_type = result.get("type", "")
    if change_type == "direct_swap":
        result["employee_name_1"] = resolve_employee(result, data, current_schedule, "_1")
        result["employee_name_2"] = resolve_employee(result, data, current_schedule, "_2")
    else:
        result["employee_name"] = resolve_employee(result, data, current_schedule)

    _validate_change(result, get_staff_names(data))
    return result


def _validate_change(change, staff_names):
    valid_types = {"unavailable","direct_swap","avoid_back_to_back",
                   "back_to_back_preference","avoid_shift","shift_preference"}
    t = change.get("type", "")
    if t not in valid_types:
        raise ValueError(f"Unknown change type: '{t}'")

    name_set = {simplify_text(n) for n in staff_names}

    def ck_name(f):
        n = change.get(f, "")
        if simplify_text(n) not in name_set:
            raise ValueError(f"Employee '{n}' not found in staff list.")

    def ck_day(f):
        d = change.get(f)
        if d and d not in set(ORDERED_DAYS):
            try:    change[f] = normalize_day(d)
            except: raise ValueError(f"Invalid day: '{d}'")

    if t == "unavailable":
        ck_name("employee_name"); ck_day("day")
    elif t == "direct_swap":
        ck_name("employee_name_1"); ck_name("employee_name_2")
        ck_day("day_1"); ck_day("day_2")
    elif t in {"avoid_back_to_back","back_to_back_preference"}:
        ck_name("employee_name")
    elif t in {"avoid_shift","shift_preference"}:
        ck_name("employee_name"); ck_day("day")


# ---------------------------------------------------------------------------
# Display helpers (CLI)
# ---------------------------------------------------------------------------

DAY_ORDER   = {d: i for i, d in enumerate(ORDERED_DAYS)}
SHIFT_ORDER = {"morning": 1, "afternoon": 2, "evening": 3}


def print_schedule(schedule, title="SCHEDULE"):
    grouped = defaultdict(list)
    for row in schedule: grouped[(row["day"], row["shift"])].append(row)
    print(f"\n{'='*(len(title)+12)}\n  {title}\n{'='*(len(title)+12)}")
    for day, shift in sorted(grouped, key=lambda x: (DAY_ORDER.get(x[0],99), SHIFT_ORDER.get(x[1].lower(),99))):
        print(f"\n{day} - {shift}")
        for row in sorted(grouped[(day,shift)], key=lambda r:(r["role"],r["employee_name"])):
            print(f"  {row['role']:<10} -> {row['employee_name']} ({row['employee_id']})")
    print("="*(len(title)+12))

def print_preferences(data):
    prefs = data.get("preferences", [])
    print("\n===== CURRENT PREFERENCES =====")
    if not prefs: print("  No preferences.")
    else:
        for i, p in enumerate(prefs, 1): print(f"  {i}. {p}")
    print("===============================\n")

def compare_schedules(old_schedule, new_schedule):
    old_set = schedule_to_assignment_set(old_schedule)
    new_set = schedule_to_assignment_set(new_schedule)
    removed, added = old_set - new_set, new_set - old_set
    print("===== CHANGES =====")
    if not removed and not added: print("  No changes.")
    else:
        for emp_id, day, shift, role in sorted(removed): print(f"  - {emp_id}: {day} {shift} {role}")
        for emp_id, day, shift, role in sorted(added):   print(f"  + {emp_id}: {day} {shift} {role}")
    print("===================\n")

def print_explanation(explanation):
    print("===== EXPLANATION =====")
    print(explanation)
    print("=======================\n")

def ask_yes_no(prompt):
    while True:
        answer  = simplify_text(input(prompt))
        compact = answer.replace(" ", "")
        if answer in YES_ALIASES or compact in YES_ALIASES: return True
        if answer in NO_ALIASES  or compact in NO_ALIASES:  return False
        print("Please enter yes/y or no/n.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Restaurant shift scheduler with NL input")
    parser.add_argument("--data", default="restaurant_data.json")
    args = parser.parse_args()

    try:
        data = load_data_from_json(args.data)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error loading data: {e}"); sys.exit(1)

    print(f"Loaded data from '{args.data}'")
    print("Generating initial schedule...")
    try:
        schedule = generate_schedule(data)
    except ValueError as e:
        print(f"Scheduling error: {e}"); sys.exit(1)

    print_schedule(schedule, title="INITIAL SCHEDULE")
    print_preferences(data)

    while True:
        if not ask_yes_no("Do you want to make an update? (y/n): "):
            print("Done."); break

        user_input = input("Describe the change in plain English: ").strip()
        if not user_input: continue

        print("[Parsing with LLM...]")
        try:
            change_request = parse_user_request(user_input, data, schedule)
        except (ValueError, RuntimeError) as e:
            print(f"[Parse Error] {e}\n"); continue

        print(f"[Parsed] {change_request}")
        try:
            updated_schedule, updated_data = update_schedule(data, schedule, change_request)
        except ValueError as e:
            print(f"[Solver Error] {e}\n"); continue

        print_schedule(updated_schedule, title="UPDATED SCHEDULE")
        compare_schedules(schedule, updated_schedule)
        explanation = generate_explanation(data, schedule, updated_schedule, change_request)
        print_explanation(explanation)
        print_preferences(updated_data)

        data     = updated_data
        schedule = updated_schedule

        if ask_yes_no("Save changes to JSON? (y/n): "):
            save_data_to_json(data, args.data)
            print(f"Saved to '{args.data}'\n")
        if ask_yes_no("Print current schedule again? (y/n): "):
            print_schedule(schedule, title="CURRENT SCHEDULE")
            print_preferences(data)


if __name__ == "__main__":
    main()
