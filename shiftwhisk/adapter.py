"""
adapter.py — Bridge between the ShiftWhisk UI data format and the solver.

The UI stores everything in a single JavaScript object `D` (persisted to
localStorage).  The solver expects a different JSON schema.  This module
handles both directions:

  ui_to_solver(D, week_offset)  →  solver_data dict  (for generate / update)
  solver_to_ui(solver_schedule, D, week_offset)  →  updated D.schedule dict

UI key conventions (from index.html):
  - D.employees[i].availability  : list of "dayIndex_shiftId" strings
      e.g. "0_sh1748000000"  →  Monday + shift with id "sh1748000000"
  - D.schedule key               : "weekOffset|dayIndex|shiftId"
  - D.staffing key               : "shiftId_role_wd" / "shiftId_role_we"
  - D.shifts[i]                  : { id, name, start, end }
  - D.employees[i]               : { id (random), employeeId (8-char unique),
                                     name, primaryRole, seniority,
                                     maxHours, availability, skills }

Solver key conventions (with_llm.py):
  - staff record     : { id, name, skills, max_hours, shift_duration_hours,
                         seniority }
  - availability row : { employee_id, day, shift, available }
  - shift_requirement: { day, shift, role, required_count }
  - schedule row     : { employee_id, employee_name, day, shift, role }
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKEND_INDICES = {5, 6}  # Saturday=5, Sunday=6 (0-indexed, Monday=0)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _shift_duration_hours(shift: Dict[str, Any]) -> float:
    """Return the duration in hours of a UI shift dict {start, end}.

    Handles overnight shifts (end < start) by adding 24 h.
    """
    try:
        sh, sm = map(int, str(shift.get("start", "0:0")).split(":"))
        eh, em = map(int, str(shift.get("end",   "0:0")).split(":"))
    except ValueError:
        return 8.0  # safe default if time strings are malformed

    minutes = (eh * 60 + em) - (sh * 60 + sm)
    if minutes <= 0:
        minutes += 24 * 60  # overnight shift
    return minutes / 60.0


def _is_weekend(day_index: int) -> bool:
    return day_index in WEEKEND_INDICES


def _day_is_closed(
    ui_data: Dict[str, Any],
    day_index: int,
    week_offset: int = 0,
) -> bool:
    import datetime
    special = ui_data.get("specialDates") or {}
    if special:
        try:
            today  = datetime.date.today()
            monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(weeks=week_offset)
            actual = monday + datetime.timedelta(days=day_index)
            dk     = actual.strftime("%Y-%m-%d")
            if dk in special and special[dk].get("closed") is not None:
                return bool(special[dk]["closed"])
        except Exception:
            pass
    hours     = (ui_data.get("restaurant") or {}).get("hours") or {}
    day_hours = hours.get(str(day_index)) or hours.get(day_index)
    if day_hours is None:
        return False
    return bool(day_hours.get("closed", False))


def _get_date_staffing_override(
    ui_data: Dict[str, Any],
    day_index: int,
    shift_name: str,
    role: str,
    week_offset: int = 0,
) -> Optional[int]:
    """Return a date-specific staffing count override, or None if not set."""
    import datetime
    special = ui_data.get("specialDates") or {}
    if not special:
        return None
    try:
        today  = datetime.date.today()
        monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(weeks=week_offset)
        actual = monday + datetime.timedelta(days=day_index)
        dk     = actual.strftime("%Y-%m-%d")
        if dk in special:
            overrides = special[dk].get("staffingOverrides") or {}
            key = shift_name + "|" + role
            if key in overrides:
                return int(overrides[key])
            # Also try case-insensitive
            for k, v in overrides.items():
                kshift, krole = k.split("|", 1)
                if kshift.lower() == shift_name.lower() and krole.lower() == role.lower():
                    return int(v)
    except Exception:
        pass
    return None


def _is_shift_disabled(
    ui_data: Dict[str, Any],
    day_index: int,
    shift_name: str,
    week_offset: int = 0,
) -> bool:
    import datetime
    sl = shift_name.lower()
    special = ui_data.get("specialDates") or {}
    if special:
        try:
            today  = datetime.date.today()
            monday = today - datetime.timedelta(days=today.weekday()) + datetime.timedelta(weeks=week_offset)
            actual = monday + datetime.timedelta(days=day_index)
            dk     = actual.strftime("%Y-%m-%d")
            if dk in special:
                ds = [s.lower() for s in (special[dk].get("disabledShifts") or [])]
                if ds:
                    return sl in ds
        except Exception:
            pass
    hours     = (ui_data.get("restaurant") or {}).get("hours") or {}
    day_hours = hours.get(str(day_index)) or hours.get(day_index)
    if day_hours:
        ds = [s.lower() for s in (day_hours.get("disabledShifts") or [])]
        if sl in ds:
            return True
    return False


# ---------------------------------------------------------------------------
# UI → Solver
# ---------------------------------------------------------------------------

def ui_to_solver(
    ui_data: Dict[str, Any],
    week_offset: int = 0,
    persisted_preferences: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Convert the UI `D` object into the dict expected by with_llm.solve_schedule.

    Parameters
    ----------
    ui_data                : The full `D` object sent from the browser (JSON-decoded).
    week_offset            : Which week the manager is viewing (0 = current week).
    persisted_preferences  : Preferences saved in D.solverCache from previous
                             chat sessions.  Merged into the returned dict so
                             Auto Schedule always applies accumulated rules.

    Returns
    -------
    A dict with keys: staff, availability, shift_requirements, preferences.
    """
    employees   = ui_data.get("employees", [])
    shifts      = ui_data.get("shifts", [])
    roles       = ui_data.get("roles", [])
    staffing    = ui_data.get("staffing", {})

    # --- Build a shift-id → shift dict for quick lookup ---
    shift_by_id: Dict[str, Dict] = {sh["id"]: sh for sh in shifts}

    # ------------------------------------------------------------------ staff
    staff: List[Dict[str, Any]] = []
    for emp in employees:
        # employeeId is the 8-char unique ID we added to the UI.
        # Fall back to the auto-generated random id if somehow missing.
        solver_id = emp.get("employeeId") or emp.get("id", "")

        # Collect all roles this employee can fill: primaryRole + additional skills.
        # Normalise to lowercase so skill matching in the solver is case-insensitive.
        primary = emp.get("primaryRole", "")
        skills: List[str] = [primary.lower()] if primary else []
        for sk in emp.get("skills", []):
            sk_lower = sk.lower() if sk else ""
            if sk_lower and sk_lower not in skills:
                skills.append(sk_lower)

        # Use the first (primary) shift duration as a representative value.
        # If the employee has multiple shifts, the adapter uses the average.
        durations = [_shift_duration_hours(shift_by_id[sh["id"]])
                     for sh in shifts if sh["id"] in shift_by_id]
        avg_duration = (sum(durations) / len(durations)) if durations else 8.0

        staff.append({
            "id":                   solver_id,
            "name":                 emp.get("name", ""),
            "skills":               skills,
            "max_hours":            float(emp.get("maxHours", 40)),
            "shift_duration_hours": avg_duration,
            "seniority":            int(emp.get("seniority", 0)),
        })

    # ------------------------------------------------------------- availability
    # UI format: employee.availability = ["dayIndex_shiftId", ...]
    # An empty list means "available for everything" in the UI —
    # we treat it the same way in the solver (mark all slots True).
    availability: List[Dict[str, Any]] = []

    for emp in employees:
        solver_id  = emp.get("employeeId") or emp.get("id", "")
        avail_keys = set(emp.get("availability", []))
        all_avail  = len(avail_keys) == 0  # empty list = no restrictions

        for di in range(7):
            if _day_is_closed(ui_data, di, week_offset):
                continue
            day_name = DAYS[di]
            for sh in shifts:
                if _is_shift_disabled(ui_data, di, sh["name"], week_offset):
                    continue
                slot_key  = f"{di}_{sh['id']}"
                is_avail  = all_avail or (slot_key in avail_keys)
                availability.append({
                    "employee_id": solver_id,
                    "day":         day_name,
                    "shift":       sh["name"],
                    "available":   is_avail,
                })

    # -------------------------------------------------------- shift_requirements
    # Expand staffing dict into one row per (day × shift × role).
    # staffing key: "shiftId_role_wd"  or  "shiftId_role_we"
    shift_requirements: List[Dict[str, Any]] = []

    for di in range(7):
        if _day_is_closed(ui_data, di, week_offset):
            continue
        day_name  = DAYS[di]
        suffix    = "we" if _is_weekend(di) else "wd"

        for sh in shifts:
            if _is_shift_disabled(ui_data, di, sh["name"], week_offset):
                continue
            for role in roles:
                # Priority: date-specific override > per-day key > weekday/weekend global
                date_override = _get_date_staffing_override(ui_data, di, sh["name"], role, week_offset)
                if date_override is not None:
                    required_count = date_override
                else:
                    per_day_key  = f"{sh['id']}_{role}_d{di}"
                    global_key   = f"{sh['id']}_{role}_{suffix}"
                    required_count = int(staffing.get(per_day_key, staffing.get(global_key, 0)))
                if required_count > 0:
                    shift_requirements.append({
                        "day":            day_name,
                        "shift":          sh["name"],
                        "role":           role.lower(),
                        "required_count": required_count,
                    })

    # ------------------------------------------------------------ preferences
    # Preferences accumulate via chat and are persisted in D.solverCache on
    # the frontend.  The caller (app.py) passes them in so they survive
    # page refreshes and Auto Schedule reruns.
    preferences: List[Dict[str, Any]] = []

    # Merge persisted preferences from previous chat sessions
    if persisted_preferences:
        preferences = list(persisted_preferences)

    return {
        "staff":              staff,
        "availability":       availability,
        "shift_requirements": shift_requirements,
        "preferences":        preferences,
    }


# ---------------------------------------------------------------------------
# Solver → UI
# ---------------------------------------------------------------------------

def solver_to_ui(
    solver_schedule: List[Dict[str, str]],
    ui_data: Dict[str, Any],
    week_offset: int = 0,
) -> Dict[str, Any]:
    """Convert a solver schedule (list of assignment rows) into D.schedule format.

    Parameters
    ----------
    solver_schedule : Output of with_llm.solve_schedule / generate_schedule.
    ui_data         : The UI `D` object (used to look up shift IDs and
                      employee internal IDs).
    week_offset     : The week the manager is currently viewing.

    Returns
    -------
    A dict that can be merged into `D.schedule` on the frontend.
    Each key is  "weekOffset|dayIndex|shiftId"
    Each value  is a list of { empId, role } objects.

    Only the cells for `week_offset` are replaced; other weeks are untouched.
    """
    # Build lookup maps for the reverse translation.
    # Use lowercase keys so matching is case-insensitive — the solver stores
    # shift names in lowercase (e.g. "afternoon") while the UI may use "Afternoon".
    shift_name_to_id: Dict[str, str] = {sh["name"].lower(): sh["id"]
                                         for sh in ui_data.get("shifts", [])}
    day_to_index: Dict[str, int]     = {name: i for i, name in enumerate(DAYS)}

    # Build a map from solver employee_id → UI internal id (emp.id)
    # so we can reference the correct employee in D.employees.
    solver_id_to_ui_id: Dict[str, str] = {}
    for emp in ui_data.get("employees", []):
        solver_id = emp.get("employeeId") or emp.get("id", "")
        solver_id_to_ui_id[solver_id] = emp["id"]  # UI's own random id

    # Group solver rows into cells
    new_schedule: Dict[str, List[Dict[str, str]]] = {}

    for row in solver_schedule:
        day_name   = row["day"]
        shift_name = row["shift"]
        role       = row["role"]
        solver_id  = row["employee_id"]

        di       = day_to_index.get(day_name)
        shift_id = shift_name_to_id.get(shift_name.lower())  # lowercase for case-insensitive match
        ui_emp_id = solver_id_to_ui_id.get(solver_id)

        # Skip rows we can't map (shouldn't happen in a consistent dataset)
        if di is None or shift_id is None or ui_emp_id is None:
            continue

        cell_key = f"{week_offset}|{di}|{shift_id}"
        new_schedule.setdefault(cell_key, [])
        new_schedule[cell_key].append({"empId": ui_emp_id, "role": role})

    return new_schedule


# ---------------------------------------------------------------------------
# Merge helper — used by app.py to patch only the current week
# ---------------------------------------------------------------------------

def merge_schedule(
    existing: Dict[str, Any],
    new_cells: Dict[str, Any],
    week_offset: int,
) -> Dict[str, Any]:
    """Return a copy of `existing` with the current week's cells replaced.

    Cells from other weeks are preserved as-is.
    """
    prefix = f"{week_offset}|"
    merged = {k: v for k, v in existing.items() if not k.startswith(prefix)}
    merged.update(new_cells)
    return merged
