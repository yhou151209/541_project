"""
Restaurant shift scheduling system with natural language input.

Setup:
    pip install ortools requests flask flask-cors

    Groq API key: https://console.groq.com/home

Run standalone CLI:
    GROQ_API_KEY=your-key python3 with_llm.py --data restaurant_data.json

Run as part of the ShiftWhisk web app:
    Start app.py instead — this module is imported by the Flask backend.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ortools.sat.python import cp_model
import requests


#  DATA LOADING & VALIDATION

def load_data_from_json(file_path: str) -> Dict[str, Any]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {file_path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    validate_data(data)
    return data


def save_data_to_json(data: Dict[str, Any], file_path: str) -> None:
    path = Path(file_path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def validate_data(data: Dict[str, Any]) -> None:
    required_top_keys = {"staff", "availability", "shift_requirements"}
    missing = required_top_keys - set(data.keys())
    if missing:
        raise ValueError(f"Missing top-level keys: {missing}")

    for s in data["staff"]:
        # max_hours replaces max_shifts; employment_type is no longer required
        for key in ["id", "name", "skills", "max_hours"]:
            if key not in s:
                raise ValueError(f"Staff record missing '{key}': {s}")

    for row in data["availability"]:
        for key in ["employee_id", "day", "shift", "available"]:
            if key not in row:
                raise ValueError(f"Availability record missing '{key}': {row}")

    for req in data["shift_requirements"]:
        for key in ["day", "shift", "role", "required_count"]:
            if key not in req:
                raise ValueError(f"Shift requirement missing '{key}': {req}")

    for pref in data.get("preferences", []):
        if "type" not in pref or "employee_name" not in pref:
            raise ValueError(f"Preference missing required fields: {pref}")


#  HELPER UTILITIES

def make_availability_lookup(availability: List[Dict[str, Any]]) -> Dict[Tuple, bool]:
    # Normalise day and shift to lowercase so matching is case-insensitive
    # regardless of how the UI or adapter capitalised them.
    return {
        (row["employee_id"], row["day"].lower(), row["shift"].lower()): bool(row["available"])
        for row in availability
    }


def get_staff_lookup(staff: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {s["id"]: s for s in staff}


def schedule_to_assignment_set(schedule: List[Dict[str, str]]) -> set:
    return {
        (row["employee_id"], row["day"], row["shift"], row["role"])
        for row in schedule
    }


def get_assignments_for_employee_shift(
    schedule: List[Dict[str, str]], employee_name: str, day: str, shift: str
) -> List[Dict[str, str]]:
    return [
        row for row in schedule
        if (row["employee_name"].lower() == employee_name.lower()
            and row["day"].lower() == day.lower()
            and row["shift"].lower() == shift.lower())
    ]


def get_employee_assignment_roles(
    schedule: List[Dict[str, str]], employee_name: str, day: str, shift: str
) -> List[str]:
    return [
        row["role"]
        for row in get_assignments_for_employee_shift(schedule, employee_name, day, shift)
    ]


def set_availability(
    data: Dict[str, Any], employee_id: str, day: str, shift: str, available: bool
) -> None:
    found = False
    for row in data["availability"]:
        if (row["employee_id"] == employee_id
                and row["day"].lower() == day.lower()
                and row["shift"].lower() == shift.lower()):
            row["available"] = available
            row["day"] = day
            row["shift"] = shift
            found = True
    if not found:
        data["availability"].append(
            {"employee_id": employee_id, "day": day, "shift": shift, "available": available}
        )


def add_preference(data: Dict[str, Any], preference: Dict[str, Any]) -> None:
    data.setdefault("preferences", []).append(preference)


def is_busy_shift(day: str, shift: str) -> bool:
    if shift == "evening":
        return True
    if day in {"Saturday", "Sunday"}:
        return True
    return False


def get_staff_names(data: Dict[str, Any]) -> List[str]:
    return [s["name"] for s in data["staff"]]

#  SOLVER

def solve_schedule(
    data: Dict[str, Any],
    preferred_schedule: List[Dict[str, str]] | None = None,
    forced_assignments: List[Tuple] | None = None,
) -> List[Dict[str, str]]:
    staff        = data["staff"]
    availability = data["availability"]
    requirements = data["shift_requirements"]
    preferences  = data.get("preferences", [])

    staff_lookup        = get_staff_lookup(staff)
    name_to_id          = {s["name"].lower(): s["id"] for s in staff}
    avail_lookup        = make_availability_lookup(availability)
    preferred_assignments = schedule_to_assignment_set(preferred_schedule or [])
    forced_assignments  = forced_assignments or []

    model = cp_model.CpModel()
    x: Dict[Tuple, cp_model.IntVar] = {}

    all_roles          = sorted({r["role"]  for r in requirements})
    all_day_shift_pairs = sorted({(r["day"], r["shift"]) for r in requirements})
    all_days           = sorted({r["day"]   for r in requirements})
    all_shifts         = sorted({r["shift"] for r in requirements})

    # Create variables
    for req in requirements:
        day, shift, role = req["day"], req["shift"], req["role"]
        for emp in staff:
            emp_id = emp["id"]
            has_skill    = role.lower() in {s.lower() for s in emp["skills"]}
            is_available = avail_lookup.get((emp_id, day.lower(), shift.lower()), False)
            var = model.NewBoolVar(f"x_{emp_id}_{day}_{shift}_{role}")
            x[(emp_id, day, shift, role)] = var
            if not has_skill or not is_available:
                model.Add(var == 0)

    # Coverage constraints
    for req in requirements:
        day, shift, role = req["day"], req["shift"], req["role"]
        model.Add(sum(x[(emp["id"], day, shift, role)] for emp in staff) == int(req["required_count"]))

    # One role per employee per shift
    for emp in staff:
        emp_id = emp["id"]
        for day, shift in all_day_shift_pairs:
            same_shift_vars = [
                x[(emp_id, day, shift, role)]
                for role in all_roles
                if (emp_id, day, shift, role) in x
            ]
            if same_shift_vars:
                model.Add(sum(same_shift_vars) <= 1)

    # Max shifts per employee — derived from max_hours / shift_duration_hours.
    # shift_duration_hours is stored on each staff record by the adapter.
    # If missing, fall back to a safe default of 40 hours / 8 hours = 5 shifts.
    max_possible = len(all_day_shift_pairs)
    total_assigned_per_emp: Dict[str, cp_model.IntVar] = {}
    for emp in staff:
        emp_id   = emp["id"]
        emp_vars = [var for key, var in x.items() if key[0] == emp_id]
        shift_hours   = float(emp.get("shift_duration_hours", 8.0))
        max_hrs       = float(emp.get("max_hours", 40.0))
        max_shifts_emp = max(1, int(max_hrs / shift_hours)) if shift_hours > 0 else 5
        model.Add(sum(emp_vars) <= max_shifts_emp)
        total_var = model.NewIntVar(0, max_possible, f"total_{emp_id}")
        model.Add(total_var == sum(emp_vars))
        total_assigned_per_emp[emp_id] = total_var

    # Forced assignments
    for assignment in forced_assignments:
        if assignment not in x:
            raise ValueError(f"Forced assignment not in model: {assignment}")
        model.Add(x[assignment] == 1)

    # Balance workload
    max_load = model.NewIntVar(0, max_possible, "max_load")
    for total_var in total_assigned_per_emp.values():
        model.Add(total_var <= max_load)

    # Stability: keep preferred assignments
    keep_vars = [var for key, var in x.items() if key in preferred_assignments] if preferred_schedule else []

    # Soft preference penalties
    penalty_terms = []
    for i, pref in enumerate(preferences):
        pref_type     = pref["type"]
        employee_name = pref["employee_name"].lower()
        if employee_name not in name_to_id:
            continue
        emp_id  = name_to_id[employee_name]
        penalty = int(pref.get("penalty", 1))

        if pref_type == "avoid_shift":
            target_day   = pref.get("day")
            target_shift = pref.get("shift")
            for role in all_roles:
                for day in all_days:
                    for shift in all_shifts:
                        if (target_day is None or day == target_day) and \
                           (target_shift is None or shift == target_shift):
                            key = (emp_id, day, shift, role)
                            if key in x:
                                penalty_terms.append(x[key] * penalty)

        elif pref_type == "avoid_back_to_back":
            for day in all_days:
                m_vars = [x[(emp_id, day, "morning", r)] for r in all_roles if (emp_id, day, "morning", r) in x]
                e_vars = [x[(emp_id, day, "evening", r)] for r in all_roles if (emp_id, day, "evening", r) in x]
                if not m_vars or not e_vars:
                    continue
                m_assigned  = model.NewBoolVar(f"m_{emp_id}_{day}_{i}")
                e_assigned  = model.NewBoolVar(f"e_{emp_id}_{day}_{i}")
                b2b         = model.NewBoolVar(f"b2b_{emp_id}_{day}_{i}")
                model.Add(sum(m_vars) >= 1).OnlyEnforceIf(m_assigned)
                model.Add(sum(m_vars) == 0).OnlyEnforceIf(m_assigned.Not())
                model.Add(sum(e_vars) >= 1).OnlyEnforceIf(e_assigned)
                model.Add(sum(e_vars) == 0).OnlyEnforceIf(e_assigned.Not())
                model.AddBoolAnd([m_assigned, e_assigned]).OnlyEnforceIf(b2b)
                model.AddBoolOr([m_assigned.Not(), e_assigned.Not()]).OnlyEnforceIf(b2b.Not())
                penalty_terms.append(b2b * penalty)

    # Busy shift penalty: prefer higher-seniority staff on busy slots.
    # Without employment_type, we use seniority as a proxy — lower seniority
    # gets a small penalty on evening / weekend shifts to nudge the solver
    # toward more experienced staff when possible.
    busy_penalties = [
        var * max(1, 4 - int(staff_lookup[emp_id].get("seniority", 0)))
        for (emp_id, day, shift, role), var in x.items()
        if is_busy_shift(day, shift) and int(staff_lookup[emp_id].get("seniority", 0)) < 2
    ]

    total_penalty = model.NewIntVar(0, 50000, "total_penalty")
    all_penalties = penalty_terms + busy_penalties
    model.Add(total_penalty == (sum(all_penalties) if all_penalties else 0))

    if keep_vars:
        model.Maximize(sum(keep_vars) * 1000 - total_penalty * 10 - max_load)
    else:
        model.Minimize(total_penalty * 10 + max_load)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ValueError("No feasible schedule found under current constraints.")

    schedule = [
        {
            "employee_id":   emp_id,
            "employee_name": staff_lookup[emp_id]["name"],
            "day":   day,
            "shift": shift,
            "role":  role,
        }
        for (emp_id, day, shift, role), var in x.items()
        if solver.Value(var) == 1
    ]
    schedule.sort(key=lambda r: (r["day"], r["shift"], r["role"], r["employee_name"]))
    return schedule


def generate_schedule(data: Dict[str, Any]) -> List[Dict[str, str]]:
    return solve_schedule(data, preferred_schedule=None)


#  UPDATE SCHEDULE

def update_schedule(
    data: Dict[str, Any],
    existing_schedule: List[Dict[str, str]],
    change: Dict[str, Any],
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
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
        matched = [s for s in data["staff"] if s["name"].strip().lower() == employee_name.strip().lower()]
        if not matched:
            raise ValueError(f"Employee '{employee_name}' not found.")
        emp_id = matched[0]["id"]

        if shift.lower() == "all" or not shift:
            # Mark employee unavailable for every shift on this day
            all_shifts = sorted({r["shift"] for r in data["shift_requirements"]})
            for sh in all_shifts:
                set_availability(updated_data, emp_id, day, sh, False)
        else:
            # Find the closest matching shift name (case-insensitive substring match)
            all_shifts = sorted({r["shift"] for r in data["shift_requirements"]})
            matched_shift = next(
                (s for s in all_shifts if shift.lower() in s.lower() or s.lower() in shift.lower()),
                shift  # fall back to whatever the LLM said
            )
            set_availability(updated_data, emp_id, day, matched_shift, False)

        return solve_schedule(updated_data, preferred_schedule=existing_schedule), updated_data

    if change_type == "direct_swap":
        name_1, day_1, shift_1 = change["employee_name_1"].strip(), change["day_1"].strip(), change["shift_1"].strip()
        name_2, day_2, shift_2 = change["employee_name_2"].strip(), change["day_2"].strip(), change["shift_2"].strip()
        m1 = [s for s in data["staff"] if s["name"].lower() == name_1.lower()]
        m2 = [s for s in data["staff"] if s["name"].lower() == name_2.lower()]
        if not m1: raise ValueError(f"Employee '{name_1}' not found.")
        if not m2: raise ValueError(f"Employee '{name_2}' not found.")
        emp_1, emp_2 = m1[0], m2[0]
        if emp_1["id"] == emp_2["id"]:
            raise ValueError("direct_swap requires two different employees.")
        roles_1 = get_employee_assignment_roles(existing_schedule, name_1, day_1, shift_1)
        roles_2 = get_employee_assignment_roles(existing_schedule, name_2, day_2, shift_2)
        if not roles_1: raise ValueError(f"{name_1} is not assigned to {day_1} {shift_1}.")
        if not roles_2: raise ValueError(f"{name_2} is not assigned to {day_2} {shift_2}.")
        if len(roles_1) != 1 or len(roles_2) != 1:
            raise ValueError("Each employee must have exactly one role in the specified shift.")
        role_1, role_2 = roles_1[0], roles_2[0]
        if role_2.lower() not in {s.lower() for s in emp_1["skills"]}:
            raise ValueError(f"{name_1} lacks skill '{role_2}' needed for the swap.")
        if role_1.lower() not in {s.lower() for s in emp_2["skills"]}:
            raise ValueError(f"{name_2} lacks skill '{role_1}' needed for the swap.")
        set_availability(updated_data, emp_1["id"], day_2, shift_2, True)
        set_availability(updated_data, emp_2["id"], day_1, shift_1, True)
        forced = [(emp_1["id"], day_2, shift_2, role_2), (emp_2["id"], day_1, shift_1, role_1)]
        return solve_schedule(updated_data, preferred_schedule=existing_schedule, forced_assignments=forced), updated_data

    if change_type == "avoid_back_to_back":
        employee_name = change["employee_name"].strip()
        if not any(s["name"].lower() == employee_name.lower() for s in data["staff"]):
            raise ValueError(f"Employee '{employee_name}' not found.")
        add_preference(updated_data, {"type": "avoid_back_to_back", "employee_name": employee_name, "penalty": int(change.get("penalty", 5))})
        return solve_schedule(updated_data, preferred_schedule=existing_schedule), updated_data

    if change_type == "avoid_shift":
        employee_name = change["employee_name"].strip()
        if not any(s["name"].lower() == employee_name.lower() for s in data["staff"]):
            raise ValueError(f"Employee '{employee_name}' not found.")
        pref: Dict[str, Any] = {"type": "avoid_shift", "employee_name": employee_name, "penalty": int(change.get("penalty", 3))}
        if change.get("day"):   pref["day"]   = change["day"].strip()
        if change.get("shift"): pref["shift"] = change["shift"].strip()
        add_preference(updated_data, pref)
        return solve_schedule(updated_data, preferred_schedule=existing_schedule), updated_data

    raise ValueError(f"Unknown change type: '{change_type}'")


#  EXPLANATION

def get_removed_and_added_assignments(
    old_schedule: List[Dict[str, str]],
    new_schedule: List[Dict[str, str]],
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    def to_set(sched):
        return {(r["employee_id"], r["employee_name"], r["day"], r["shift"], r["role"]) for r in sched}

    removed = to_set(old_schedule) - to_set(new_schedule)
    added   = to_set(new_schedule) - to_set(old_schedule)

    def to_rows(s):
        return [{"employee_id": a, "employee_name": b, "day": c, "shift": d, "role": e}
                for a, b, c, d, e in sorted(s)]

    return to_rows(removed), to_rows(added)


def generate_explanation(
    data_before: Dict[str, Any],
    old_schedule: List[Dict[str, str]],
    new_schedule: List[Dict[str, str]],
    change: Dict[str, Any],
) -> str:
    change_type  = change.get("type", "").strip()
    removed_rows, added_rows = get_removed_and_added_assignments(old_schedule, new_schedule)
    lines: List[str] = []

    if change_type == "unavailable":
        employee_name, day, shift = change["employee_name"], change["day"], change["shift"]
        lines.append(f"{employee_name} was removed because they are unavailable for {day} {shift}.")
        replacements = [r for r in added_rows if r["day"].lower() == day.lower() and r["shift"].lower() == shift.lower()]
        for row in replacements:
            emp_info = next((s for s in data_before["staff"] if s["name"].lower() == row["employee_name"].lower()), None)
            if emp_info and emp_info["employment_type"] == "full-time" and is_busy_shift(row["day"], row["shift"]):
                lines.append(f"{row['employee_name']} was assigned because they are available, qualified as a {row['role']}, and full-time staff are preferred on busy shifts.")
            else:
                lines.append(f"{row['employee_name']} was assigned because they are available and qualified as a {row['role']}.")
        if not replacements:
            lines.append("The schedule was re-optimized to keep the shift covered with available qualified staff.")

    elif change_type == "direct_swap":
        lines.append(f"A direct swap was performed between {change['employee_name_1']} ({change['day_1']} {change['shift_1']}) and {change['employee_name_2']} ({change['day_2']} {change['shift_2']}).")
        lines.append("The swap was accepted because both employees were assigned to those shifts and remained qualified for the exchanged roles.")

    elif change_type == "avoid_back_to_back":
        employee_name = change["employee_name"]
        before_count = after_count = 0
        for day in {r["day"] for r in old_schedule + new_schedule}:
            def has_shift(sched, name, d, s):
                return any(r["employee_name"].lower() == name.lower() and r["day"] == d and r["shift"] == s for r in sched)
            if has_shift(old_schedule, employee_name, day, "morning") and has_shift(old_schedule, employee_name, day, "evening"):
                before_count += 1
            if has_shift(new_schedule, employee_name, day, "morning") and has_shift(new_schedule, employee_name, day, "evening"):
                after_count += 1
        lines.append(f"A preference was added to avoid assigning {employee_name} to both morning and evening on the same day.")
        lines.append(f"Back-to-back assignments for {employee_name} changed from {before_count} to {after_count}.")
        lines.append("The schedule was re-optimized to reduce back-to-back assignments when possible.")

    elif change_type == "avoid_shift":
        employee_name = change["employee_name"]
        target_day    = change.get("day")
        target_shift  = change.get("shift")

        def match_target(row):
            if row["employee_name"].lower() != employee_name.lower(): return False
            if target_day   and row["day"]   != target_day:   return False
            if target_shift and row["shift"] != target_shift: return False
            return True

        before_count = sum(1 for r in old_schedule if match_target(r))
        after_count  = sum(1 for r in new_schedule if match_target(r))

        if target_day and target_shift:
            lines.append(f"A preference was added to avoid assigning {employee_name} to {target_day} {target_shift} shifts.")
            lines.append(f"Assignments for that slot changed from {before_count} to {after_count}.")
        elif target_day:
            lines.append(f"A preference was added to avoid assigning {employee_name} on {target_day}.")
            lines.append(f"Assignments on {target_day} changed from {before_count} to {after_count}.")
        elif target_shift:
            lines.append(f"A preference was added to avoid assigning {employee_name} to {target_shift} shifts.")
            lines.append(f"{target_shift.capitalize()} assignments for {employee_name} changed from {before_count} to {after_count}.")
        else:
            lines.append(f"A general avoidance preference was added for {employee_name}.")
        lines.append("The schedule was re-optimized to reduce those assignments when possible.")

    else:
        lines.append("The schedule was updated and re-optimized based on the requested change.")

    # Note on busy-shift preference: solver prefers senior staff on busy slots.
    busy_added = [r for r in added_rows if is_busy_shift(r["day"], r["shift"])]
    if busy_added:
        lines.append("Busy shifts prefer senior staff when possible, but availability and skill constraints always take priority.")

    if removed_rows or added_rows:
        lines.append("Other assignments were kept unchanged when possible.")

    return "\n".join(lines)


#  LLM PARSER 

# ---------------------------------------------------------------------------
# LLM — Groq backend (Gemini kept as commented-out alternative above)
# ---------------------------------------------------------------------------

_GROQ_MODEL   = "llama-3.3-70b-versatile"
_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Gemini alternative (uncomment _call_gemini below and swap the reference):
# _GEMINI_MODEL   = "gemini-2.0-flash"
# _GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent"

_SYSTEM_PROMPT_TEMPLATE = """You are a scheduling assistant. Convert the user's natural language request into a JSON object for a restaurant scheduling system.

STAFF — each entry shows  NAME (ID):
{staff_info}

VALID DAYS: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
VALID SHIFT NAMES: {shift_names}

IDENTIFYING AN EMPLOYEE — three options, in order of preference:
1. Use "employee_id" if the user quoted an 8-character ID (e.g. "A3F9B2C1").
2. Use "employee_name" if the user said a name.  If the name is ambiguous
   (two employees share it), return {{"error":"Ambiguous name — please use the employee ID"}}.
3. Use "slot_lookup": {{"day":"DAY","shift":"SHIFT","role":"ROLE"}} if the user referred
   to someone by their current slot (e.g. "the Chef on Monday morning").
   Only use this when the user did NOT give a name or ID.
   The caller will resolve the slot to a real employee before running the solver.

For all employee references in the JSON, include EITHER:
  "employee_id": "XXXXXXXX"    ← preferred when user gave an ID
  OR "employee_name": "NAME"   ← when user gave a name
  OR "slot_lookup": {{...}}     ← when user described the slot

SHIFT RULES:
- If the user specifies a shift (e.g. "morning", "evening"), use the closest matching shift name from VALID SHIFT NAMES.
- If the user does NOT mention a specific shift but mentions a day, set "shift": "all" — this means the employee is unavailable for ALL shifts on that day.
- Matching is case-insensitive. "morning" matches "Morning Shift", "Morning", etc.

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no explanation, no extra text.

Supported request types:

1. unavailable — employee cannot work a shift or a whole day
   With specific shift: {{"type":"unavailable","employee_name":"NAME","day":"DAY","shift":"SHIFT NAME"}}
   Whole day:           {{"type":"unavailable","employee_name":"NAME","day":"DAY","shift":"all"}}

2. direct_swap — two employees swap their assigned shifts
   {{"type":"direct_swap",
     "employee_name_1":"NAME1","day_1":"DAY1","shift_1":"SHIFT1",
     "employee_name_2":"NAME2","day_2":"DAY2","shift_2":"SHIFT2"}}

3. avoid_back_to_back — avoid assigning both morning and evening on the same day
   {{"type":"avoid_back_to_back","employee_name":"NAME","penalty":5}}

4. avoid_shift — soft preference to avoid a particular shift/day combination
   {{"type":"avoid_shift","employee_name":"NAME","day":"DAY","shift":"SHIFT","penalty":3}}
   (omit "day" or "shift" if not specified by the user)

EXAMPLES:
User: "Julia cannot work on Monday"
JSON: {{"type":"unavailable","employee_name":"Julia","day":"Monday","shift":"all"}}

User: "Alice can't work Monday morning"
JSON: {{"type":"unavailable","employee_name":"Alice","day":"Monday","shift":"Morning"}}

User: "A3F9B2C1 can't come Sunday"
JSON: {{"type":"unavailable","employee_id":"A3F9B2C1","day":"Sunday","shift":"all"}}

User: "Ian can't come Sunday evening"
JSON: {{"type":"unavailable","employee_name":"Ian","day":"Sunday","shift":"Evening"}}

User: "The Chef on Monday morning can't work Tuesday"
JSON: {{"type":"unavailable","slot_lookup":{{"day":"Monday","shift":"Morning","role":"Chef"}},"day":"Tuesday","shift":"all"}}

User: "Brian wants to swap his Monday evening shift with Ian's Tuesday evening"
JSON: {{"type":"direct_swap","employee_name_1":"Brian","day_1":"Monday","shift_1":"Evening","employee_name_2":"Ian","day_2":"Tuesday","shift_2":"Evening"}}

User: "Avoid giving Eric back-to-back shifts"
JSON: {{"type":"avoid_back_to_back","employee_name":"Eric","penalty":5}}

User: "Try not to assign Alice to morning shifts"
JSON: {{"type":"avoid_shift","employee_name":"Alice","shift":"Morning","penalty":3}}

If the request is ambiguous or missing required information, return:
{{"error":"REASON"}}"""


def _call_groq(system_prompt: str, user_message: str) -> str:
    """Send a prompt to Groq and return the raw text response."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY not set.\n"
            "Get a free key at: https://console.groq.com\n"
            "  Mac/Linux: export GROQ_API_KEY=your-key"
        )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": _GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        "temperature": 0,
        "max_tokens": 512,
    }
    resp = requests.post(_GROQ_API_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def get_staff_info(data: Dict[str, Any]) -> str:
    """Return a compact staff roster string for injection into the LLM prompt.

    Format per line:  Name (ID)
    This lets the LLM recognise both names and 8-char employee IDs.
    """
    return "\n".join(f"{s['name']} ({s['id']})" for s in data["staff"])


def get_shift_names(data: Dict[str, Any]) -> str:
    """Return comma-separated shift names taken from the solver data.

    The adapter maps UI shift names directly, so we pass them through here
    so the LLM uses the exact same strings the schedule grid uses.
    """
    names = sorted({r["shift"] for r in data.get("shift_requirements", [])})
    return ", ".join(names) if names else "Morning Shift, Evening Shift"


def resolve_employee(
    change: Dict[str, Any],
    data: Dict[str, Any],
    current_schedule: List[Dict[str, str]],
    field_prefix: str = "",
) -> str:
    """Resolve an employee reference in a change dict to a canonical name.

    The LLM may identify an employee via:
      - employee_id  (8-char unique ID)  → highest priority
      - employee_name                    → matched case-insensitively
      - slot_lookup  {day, shift, role}  → only valid if exactly one person
                                           occupies that slot in the current schedule

    Returns the canonical employee name string (as stored in data["staff"]).
    Raises ValueError with a user-friendly message on any ambiguity or miss.

    `field_prefix` handles direct_swap where keys are suffixed _1 / _2.
    """
    pfx = field_prefix  # e.g. "" or "_1" or "_2"

    # --- Option 1: employee_id ---
    # Check both plain key and suffixed key (e.g. "employee_id_1")
    emp_id_val = (change.get("employee_id") or change.get(f"employee_id{pfx}"))
    if emp_id_val:
        emp_id_val = emp_id_val.strip().upper()
        match = next((s for s in data["staff"] if s["id"].upper() == emp_id_val), None)
        if not match:
            raise ValueError(f"No employee found with ID '{emp_id_val}'.")
        return match["name"]

    # --- Option 2: employee_name ---
    # Check both plain key and suffixed key (e.g. "employee_name_1")
    name_val = (change.get("employee_name") or change.get(f"employee_name{pfx}"))
    if name_val:
        name_lower = name_val.strip().lower()
        matches = [s for s in data["staff"] if s["name"].lower() == name_lower]
        if not matches:
            raise ValueError(f"Employee '{name_val}' not found in staff list.")
        if len(matches) > 1:
            ids = ", ".join(s["id"] for s in matches)
            raise ValueError(
                f"Multiple employees named '{name_val}' found. "
                f"Please use their employee ID instead: {ids}"
            )
        return matches[0]["name"]

    # --- Option 3: slot_lookup ---
    # Check both plain key and suffixed key (e.g. "slot_lookup_1")
    slot = change.get("slot_lookup") or change.get(f"slot_lookup{pfx}")
    if slot:
        day   = slot.get("day", "").strip()
        shift = slot.get("shift", "").strip()
        role  = slot.get("role", "").strip()
        hits  = [
            r for r in current_schedule
            if r["day"].lower()   == day.lower()
            and r["shift"].lower() == shift.lower()
            and r["role"].lower()  == role.lower()
        ]
        if not hits:
            raise ValueError(
                f"No one is assigned as {role} on {day} {shift} in the current schedule."
            )
        if len(hits) > 1:
            names = ", ".join(r["employee_name"] for r in hits)
            raise ValueError(
                f"Multiple employees in that slot ({names}). "
                "Please refer to them by name or ID."
            )
        return hits[0]["employee_name"]

    raise ValueError(
        "Could not identify the employee. "
        "Please provide a name, employee ID, or shift+role reference."
    )


def parse_user_request(
    user_input: str,
    data: Dict[str, Any],
    current_schedule: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Convert a natural language string into a validated change dict.

    Steps:
      1. Build a context-aware prompt from live staff/shift data.
      2. Call the LLM (Groq).
      3. Parse and strip any markdown fences from the response.
      4. Resolve employee references (ID / name / slot_lookup) to canonical names.
      5. Validate the resulting change dict.
    """
    staff_info  = get_staff_info(data)
    shift_names = get_shift_names(data)
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        staff_info=staff_info,
        shift_names=shift_names,
    )

    raw = _call_groq(system_prompt, user_input).strip()
    # Strip markdown code fences the LLM sometimes adds
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON output:\n{raw}\nError: {e}")

    if "error" in result:
        raise ValueError(f"LLM could not parse request: {result['error']}")

    # Resolve all employee references to canonical names before validation
    change_type = result.get("type", "")

    if change_type == "direct_swap":
        # Resolve each employee independently.
        # Pass the full result dict + the suffix so resolve_employee can find
        # both "employee_name_1" and plain "employee_name" style keys.
        result["employee_name_1"] = resolve_employee(
            result, data, current_schedule, field_prefix="_1"
        )
        result["employee_name_2"] = resolve_employee(
            result, data, current_schedule, field_prefix="_2"
        )
    else:
        result["employee_name"] = resolve_employee(result, data, current_schedule)

    staff_names = get_staff_names(data)
    _validate_change(result, staff_names)
    return result


def _validate_change(change: Dict[str, Any], staff_names: List[str]) -> None:
    """Validate a parsed change dict before passing it to update_schedule.

    Shift names are no longer hardcoded to morning/evening — the adapter
    uses the actual UI shift names, so we only check that days are valid.
    """
    valid_days  = {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"}
    valid_types = {"unavailable","direct_swap","avoid_back_to_back","avoid_shift"}

    t = change.get("type", "")
    if t not in valid_types:
        raise ValueError(f"Unknown change type: '{t}'")

    name_set = {n.lower() for n in staff_names}

    def ck_name(f: str) -> None:
        n = change.get(f, "")
        if n.lower() not in name_set:
            raise ValueError(f"Employee '{n}' not found in staff list.")

    def ck_day(f: str) -> None:
        d = change.get(f)
        if d and d not in valid_days:
            raise ValueError(f"Invalid day: '{d}'")

    if t == "unavailable":
        ck_name("employee_name"); ck_day("day")
    elif t == "direct_swap":
        ck_name("employee_name_1"); ck_name("employee_name_2")
        ck_day("day_1"); ck_day("day_2")
    elif t == "avoid_back_to_back":
        ck_name("employee_name")
    elif t == "avoid_shift":
        ck_name("employee_name"); ck_day("day")


#  DISPLAY HELPERS

DAY_ORDER   = {"Monday":1,"Tuesday":2,"Wednesday":3,"Thursday":4,"Friday":5,"Saturday":6,"Sunday":7}
SHIFT_ORDER = {"morning":1,"evening":2}


def print_schedule(schedule: List[Dict[str, str]], title: str = "SCHEDULE") -> None:
    grouped = defaultdict(list)
    for row in schedule:
        grouped[(row["day"], row["shift"])].append(row)
    print(f"\n===== {title} =====")
    for day, shift in sorted(grouped, key=lambda x: (DAY_ORDER.get(x[0], 99), SHIFT_ORDER.get(x[1], 99))):
        print(f"\n{day} - {shift}")
        for row in sorted(grouped[(day, shift)], key=lambda r: (r["role"], r["employee_name"])):
            print(f"  {row['role']:<8} -> {row['employee_name']} ({row['employee_id']})")
    print("=" * (len(title) + 12))


def print_preferences(data: Dict[str, Any]) -> None:
    prefs = data.get("preferences", [])
    print("\n===== CURRENT PREFERENCES =====")
    if not prefs:
        print("  No preferences set.")
    else:
        for i, pref in enumerate(prefs, 1):
            print(f"  {i}. {pref}")
    print("================================\n")


def compare_schedules(old_schedule: List[Dict[str, str]], new_schedule: List[Dict[str, str]]) -> None:
    old_set = schedule_to_assignment_set(old_schedule)
    new_set = schedule_to_assignment_set(new_schedule)
    removed = old_set - new_set
    added   = new_set - old_set
    print("===== CHANGES =====")
    if not removed and not added:
        print("  No changes.")
    else:
        for emp_id, day, shift, role in sorted(removed):
            print(f"  - {emp_id}: {day} {shift} {role}")
        for emp_id, day, shift, role in sorted(added):
            print(f"  + {emp_id}: {day} {shift} {role}")
    print("===================\n")


def print_explanation(explanation: str) -> None:
    print("===== EXPLANATION =====")
    print(explanation)
    print("=======================\n")


#  MAIN LOOP

def ask_yes_no(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in {"y", "yes"}: return True
        if answer in {"n", "no"}:  return False
        print("Please enter y or n.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restaurant shift scheduler with NL input")
    parser.add_argument(
        "--data",
        default="restaurant_data.json",
        help="Path to restaurant_data.json (default: restaurant_data.json)"
    )
    args = parser.parse_args()

    # Load data
    try:
        data = load_data_from_json(args.data)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error loading data: {e}")
        sys.exit(1)

    print(f"Loaded data from '{args.data}'")
    print("Generating initial schedule...")

    try:
        schedule = generate_schedule(data)
    except ValueError as e:
        print(f"Scheduling error: {e}")
        sys.exit(1)

    print_schedule(schedule, title="INITIAL SCHEDULE")
    print_preferences(data)

    staff_names = get_staff_names(data)

    while True:
        if not ask_yes_no("Do you want to make an update? (y/n): "):
            print("Done.")
            break

        user_input = input("Describe the change in plain English: ").strip()
        if not user_input:
            continue

        # Parse with LLM — pass full data and current schedule so
        # resolve_employee can handle ID / name / slot_lookup references.
        print("[Parsing with LLM...]")
        try:
            change_request = parse_user_request(user_input, data, schedule)
        except (ValueError, RuntimeError) as e:
            print(f"[Parse Error] {e}\n")
            continue

        print(f"[Parsed] {change_request}")

        # Run solver
        try:
            updated_schedule, updated_data = update_schedule(data, schedule, change_request)
        except ValueError as e:
            print(f"[Solver Error] {e}\n")
            continue

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