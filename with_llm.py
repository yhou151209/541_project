"""
Restaurant shift scheduling system with natural language input.

Setup:
    pip install ortools requests

    Get a free Gemini API key at: https://aistudio.google.com/app/apikey
    Groq: https://console.groq.com/home 

Run:
    GEMINI_API_KEY=your-key python3 main.py
    GEMINI_API_KEY=your-key python3 main.py --data path/to/restaurant_data.json
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
        for key in ["id", "name", "skills", "max_shifts", "employment_type"]:
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
    return {
        (row["employee_id"], row["day"], row["shift"]): bool(row["available"])
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
            has_skill    = role in set(emp["skills"])
            is_available = avail_lookup.get((emp_id, day, shift), False)
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

    # Max shifts per employee
    max_possible = len(all_day_shift_pairs)
    total_assigned_per_emp: Dict[str, cp_model.IntVar] = {}
    for emp in staff:
        emp_id   = emp["id"]
        emp_vars = [var for key, var in x.items() if key[0] == emp_id]
        model.Add(sum(emp_vars) <= int(emp["max_shifts"]))
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

    # Busy shift penalty: prefer full-time staff
    busy_penalties = [
        var * 4
        for (emp_id, day, shift, role), var in x.items()
        if is_busy_shift(day, shift) and staff_lookup[emp_id]["employment_type"] != "full-time"
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
        day, shift    = change["day"].strip(), change["shift"].strip()
        matched = [s for s in data["staff"] if s["name"].lower() == employee_name.lower()]
        if not matched:
            raise ValueError(f"Employee '{employee_name}' not found.")
        set_availability(updated_data, matched[0]["id"], day, shift, False)
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
        if role_2 not in set(emp_1["skills"]):
            raise ValueError(f"{name_1} lacks skill '{role_2}' needed for the swap.")
        if role_1 not in set(emp_2["skills"]):
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

    full_time_staff = {s["name"] for s in data_before["staff"] if s["employment_type"] == "full-time"}
    busy_added      = [r for r in added_rows if is_busy_shift(r["day"], r["shift"])]
    if busy_added:
        ft_busy = [r for r in busy_added if r["employee_name"] in full_time_staff]
        if ft_busy:
            names = ", ".join(sorted({r["employee_name"] for r in ft_busy}))
            lines.append(f"Busy shifts prefer full-time staff when possible, which influenced assignments such as {names}.")
        else:
            lines.append("Busy shifts prefer full-time staff when possible, but availability and skill constraints still had to be satisfied.")

    if removed_rows or added_rows:
        lines.append("Other assignments were kept unchanged when possible.")

    return "\n".join(lines)


#  LLM PARSER 

#_GEMINI_MODEL   = "gemini-2.0-flash"
#_GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent"

_GROQ_MODEL   = "llama-3.3-70b-versatile"
_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM_PROMPT_TEMPLATE = """You are a scheduling assistant. Convert the user's natural language request into a JSON object for a restaurant scheduling system.

STAFF NAMES (use exactly as listed, match case-insensitively):
{staff_names}

VALID DAYS: Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
VALID SHIFTS: morning, evening

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no explanation, no extra text.

Supported types:

1. unavailable — employee cannot work a specific shift
   {{"type":"unavailable","employee_name":"NAME","day":"DAY","shift":"SHIFT"}}

2. direct_swap — two employees swap their shifts
   {{"type":"direct_swap","employee_name_1":"NAME1","day_1":"DAY1","shift_1":"SHIFT1","employee_name_2":"NAME2","day_2":"DAY2","shift_2":"SHIFT2"}}

3. avoid_back_to_back — avoid assigning both morning and evening on the same day
   {{"type":"avoid_back_to_back","employee_name":"NAME","penalty":5}}

4. avoid_shift — avoid a shift type / day / day+shift combo
   {{"type":"avoid_shift","employee_name":"NAME","day":"DAY","shift":"SHIFT","penalty":3}}
   (omit "day" or "shift" if not specified)

EXAMPLES:
User: "Ian can't come Sunday evening"
JSON: {{"type":"unavailable","employee_name":"Ian","day":"Sunday","shift":"evening"}}

User: "Brian wants to swap his Monday evening shift with Ian's Tuesday evening"
JSON: {{"type":"direct_swap","employee_name_1":"Brian","day_1":"Monday","shift_1":"evening","employee_name_2":"Ian","day_2":"Tuesday","shift_2":"evening"}}

User: "Brian wants to swap his Monday evening shift with Ian's Tuesday evening"
JSON: {{"type":"direct_swap","employee_name_1":"Brian","day_1":"Monday","shift_1":"evening","employee_name_2":"Ian","day_2":"Tuesday","shift_2":"evening"}}

User: "Avoid giving Eric back-to-back shifts"
JSON: {{"type":"avoid_back_to_back","employee_name":"Eric","penalty":5}}

User: "Try not to assign Alice to morning shifts"
JSON: {{"type":"avoid_shift","employee_name":"Alice","shift":"morning","penalty":3}}

User: "Avoid assigning Hannah on Sunday"
JSON: {{"type":"avoid_shift","employee_name":"Hannah","day":"Sunday","penalty":3}}

User: "Avoid assigning Brian to Saturday evening"
JSON: {{"type":"avoid_shift","employee_name":"Brian","day":"Saturday","shift":"evening","penalty":3}}

CRITICAL RULE: DO NOT guess, infer, or hallucinate missing days or shifts. If the user does not explicitly state the day and shift for ALL employees involved, you MUST return an error:
{{"error":"missing required information"}}"""


"""
def _call_gemini(system_prompt: str, user_message: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set.\n"
            "Get a free key at: https://aistudio.google.com/app/apikey\n"
            "  Windows:   set GEMINI_API_KEY=your-key\n"
            "  Mac/Linux: export GEMINI_API_KEY=your-key"
        )
    body = {
        "contents": [{"parts": [{"text": f"{system_prompt}\n\nUser request: {user_message}"}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 256},
    }
    resp = requests.post(_GEMINI_API_URL, params={"key": api_key}, json=body, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Gemini response: {data}") from e
"""
def _call_gemini(system_prompt: str, user_message: str) -> str:
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
        "max_tokens": 256,
    }
    resp = requests.post(_GROQ_API_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]

def parse_user_request(user_input: str, staff_names: List[str]) -> Dict[str, Any]:
    """Convert a natural language string into a change dict."""
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(staff_names=", ".join(staff_names))
    raw = _call_gemini(system_prompt, user_input).strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw).strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM returned non-JSON output:\n{raw}\nError: {e}")

    if "error" in result:
        raise ValueError(f"LLM could not parse request: {result['error']}")

    _validate_change(result, staff_names)
    return result


def _validate_change(change: Dict[str, Any], staff_names: List[str]) -> None:
    valid_days   = {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"}
    valid_shifts = {"morning","evening"}
    valid_types  = {"unavailable","direct_swap","avoid_back_to_back","avoid_shift"}

    t = change.get("type", "")
    if t not in valid_types:
        raise ValueError(f"Unknown change type: '{t}'")

    name_set = {n.lower() for n in staff_names}

    def ck_name(f):
        n = change.get(f, "")
        if not n:
            raise ValueError(f"Missing required employee name.")
        if n.lower() not in name_set:
            raise ValueError(f"Employee '{n}' not found in staff list.")

    # Added a 'required' parameter that defaults to True
    def ck_day(f, required=True):
        d = change.get(f)
        if required and not d:
            raise ValueError(f"Missing required day information.")
        if d and d not in valid_days:
            raise ValueError(f"Invalid day: '{d}'")

    # Added a 'required' parameter that defaults to True
    def ck_shift(f, required=True):
        s = change.get(f)
        if required and not s:
            raise ValueError(f"Missing required shift information.")
        if s and s not in valid_shifts:
            raise ValueError(f"Invalid shift: '{s}'")

    if t == "unavailable":
        ck_name("employee_name")
        ck_day("day") 
        ck_shift("shift")
    elif t == "direct_swap":
        ck_name("employee_name_1"); ck_name("employee_name_2")
        ck_day("day_1"); ck_day("day_2")
        ck_shift("shift_1"); ck_shift("shift_2")
    elif t == "avoid_back_to_back":
        ck_name("employee_name")
    elif t == "avoid_shift":
        ck_name("employee_name")
        # Day and shift are optional for this specific intent
        ck_day("day", required=False) 
        ck_shift("shift", required=False)


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

        # Parse with LLM
        print("[Parsing with LLM...]")
        try:
            change_request = parse_user_request(user_input, staff_names)
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
