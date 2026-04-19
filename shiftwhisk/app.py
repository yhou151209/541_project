"""
app.py — ShiftWhisk Flask backend.

Exposes three REST endpoints consumed by index.html:

  POST /api/schedule/generate
      Runs the solver from scratch using the current UI data.
      Body : { "uiData": <D object>, "weekOffset": <int> }
      Reply: { "schedule": <D.schedule patch>, "message": <str> }

  POST /api/chat
      Accepts a natural-language message, calls the LLM to parse it,
      then reruns the solver with the resulting change applied.
      Body : { "uiData": <D object>, "weekOffset": <int>,
               "message": <str>, "solverData": <solver dict | null>,
               "solverSchedule": <list | null> }
      Reply: { "schedule": <D.schedule patch>, "reply": <str>,
               "solverData": <updated solver dict>,
               "solverSchedule": <updated solver schedule> }

  GET  /api/state
      Health-check / returns the server's in-memory solver state.
      Reply: { "hasSolverData": <bool> }

Setup:
    pip install flask flask-cors ortools requests

Run:
    GROQ_API_KEY=your-key python app.py
"""

import os
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

from adapter import ui_to_solver, solver_to_ui, merge_schedule
from with_llm import (
    generate_schedule,
    update_schedule,
    parse_user_request,
    generate_explanation,
)

app = Flask(__name__)
CORS(app)  # allow the HTML file to call the API from any origin

# ---------------------------------------------------------------------------
# In-memory state
# Holds the last solver-format data + schedule so the /api/chat endpoint can
# apply incremental changes without the UI having to re-send everything.
# This is per-process; a restart clears it (intentional for this version).
# ---------------------------------------------------------------------------
_solver_data: Optional[Dict[str, Any]] = None
_solver_schedule: Optional[List[Dict]] = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _error(message: str, status: int = 400):
    """Return a JSON error response."""
    return jsonify({"error": message}), status


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/schedule/generate", methods=["POST"])
def generate():
    """Run the solver from scratch and return a D.schedule patch.

    The UI sends its full `D` object; the adapter converts it to solver
    format, the solver runs, and the result is translated back to the UI's
    schedule key format.
    """
    global _solver_data, _solver_schedule

    body = request.get_json(silent=True) or {}
    ui_data     = body.get("uiData")
    week_offset = int(body.get("weekOffset", 0))
    # solverCache is the persisted solver state from localStorage.
    # It may contain accumulated preferences set via chat in previous sessions.
    solver_cache = body.get("solverCache") or {}

    if not ui_data:
        return _error("Missing 'uiData' in request body.")

    # Extract persisted preferences from the cached solver data
    persisted_prefs = []
    cached_data = solver_cache.get("solverData") or {}
    if cached_data and isinstance(cached_data.get("preferences"), list):
        persisted_prefs = cached_data["preferences"]

    # Convert UI data → solver format, merging any persisted preferences
    try:
        solver_data = ui_to_solver(ui_data, week_offset,
                                   persisted_preferences=persisted_prefs)
    except Exception as exc:
        return _error(f"Data conversion error: {exc}")

    # Run the solver
    try:
        solver_sched = generate_schedule(solver_data)
    except ValueError as exc:
        return _error(f"Solver error: {exc}")

    # Save state for subsequent /api/chat calls
    _solver_data     = solver_data
    _solver_schedule = solver_sched

    # Convert solver schedule → UI schedule cells
    new_cells = solver_to_ui(solver_sched, ui_data, week_offset)

    # Merge with the existing schedule (preserve other weeks)
    existing  = ui_data.get("schedule", {})
    patched   = merge_schedule(existing, new_cells, week_offset)

    return jsonify({
        "schedule":      patched,         # full updated D.schedule
        "solverData":    solver_data,     # echo back so UI can cache it
        "solverSchedule": solver_sched,   # echo back for UI cache
        "message": (
            f"Schedule generated: {len(solver_sched)} assignment"
            f"{'s' if len(solver_sched) != 1 else ''} across "
            f"{len({r['day'] for r in solver_sched})} day(s)."
        ),
    })


@app.route("/api/chat", methods=["POST"])
def chat():
    """Parse a natural-language message and apply the requested change.

    The UI sends its full `D` object plus the cached solver state from the
    last generate/chat call.  If no solver state is cached, the endpoint
    runs generate_schedule first to bootstrap the current schedule.
    """
    global _solver_data, _solver_schedule

    body = request.get_json(silent=True) or {}
    ui_data       = body.get("uiData")
    week_offset   = int(body.get("weekOffset", 0))
    message       = (body.get("message") or "").strip()
    # The UI echoes back the solver state it received from the last call,
    # including any accumulated preferences from chat history.
    client_solver_data  = body.get("solverData")
    client_solver_sched = body.get("solverSchedule")
    # solverCache is the full persisted state from localStorage
    solver_cache = body.get("solverCache") or {}

    if not ui_data:
        return _error("Missing 'uiData' in request body.")
    if not message:
        return _error("Missing 'message' in request body.")

    # Use client-provided solver state if available; fall back to server memory.
    # Prefer the inline solverData (most recent) over the cache.
    solver_data  = client_solver_data  or _solver_data
    solver_sched = client_solver_sched or _solver_schedule

    # If still no solver state, bootstrap from UI data + any persisted prefs
    if solver_data is None or solver_sched is None:
        persisted_prefs = []
        cached_data = solver_cache.get("solverData") or {}
        if cached_data and isinstance(cached_data.get("preferences"), list):
            persisted_prefs = cached_data["preferences"]
        try:
            solver_data  = ui_to_solver(ui_data, week_offset,
                                        persisted_preferences=persisted_prefs)
            solver_sched = generate_schedule(solver_data)
        except (ValueError, Exception) as exc:
            return _error(
                f"No existing schedule found and auto-generate failed: {exc}. "
                "Please click Auto Schedule first."
            )

    # --- Parse the natural-language message ---
    # Inject today + explicit next-weekday dates so LLM never miscalculates
    import datetime as _dt
    _today = _dt.date.today()
    today_str = _today.strftime("%Y-%m-%d")
    today_dow = _today.strftime("%A")
    _day_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    _next_days = {}
    for _i, _dn in enumerate(_day_names):
        _delta = (_i - _today.weekday()) % 7
        _delta = _delta if _delta > 0 else 7
        _next_days[_dn] = (_today + _dt.timedelta(days=_delta)).strftime("%Y-%m-%d")
    _next_str = ", ".join(f"next {k}={v}" for k, v in _next_days.items())
    augmented_message = f"[Today is {today_str} ({today_dow}). {_next_str}] {message}"
    try:
        change = parse_user_request(augmented_message, solver_data, solver_sched)
    except (ValueError, RuntimeError) as exc:
        # Return a friendly bot reply rather than an HTTP error so the
        # chat window can display it directly.
        return jsonify({
            "reply":         str(exc),
            "schedule":      ui_data.get("schedule", {}),
            "solverData":    solver_data,
            "solverSchedule": solver_sched,
        })

    # --- Schedule query: answer directly without running the solver ---
    if change.get("type") == "schedule_query":
        query_day   = change.get("day")
        query_shift = change.get("shift")
        query_name  = change.get("employee_name")

        hits = [r for r in solver_sched if
            (not query_day   or r["day"].lower()           == query_day.lower()) and
            (not query_shift or r["shift"].lower()          == query_shift.lower()) and
            (not query_name  or r["employee_name"].lower()  == query_name.lower())]

        if not hits:
            scope = " ".join(filter(None, [query_day, query_shift]))
            if query_name:
                where = (" on " + scope) if scope else " this week"
                reply = query_name + " has no assignments" + where + "."
            else:
                where = (" for " + scope) if scope else ""
                reply = "Nobody is scheduled" + where + "."
        else:
            from collections import defaultdict
            if query_name:
                lines = [query_name + "'s assignments:"]
                for r in sorted(hits, key=lambda x: (x["day"], x["shift"])):
                    lines.append("  - " + r["day"] + " " + r["shift"] + " as " + r["role"])
                reply = chr(10).join(lines)
            else:
                grouped = defaultdict(list)
                for r in hits:
                    grouped[(r["day"], r["shift"])].append(r["employee_name"] + " (" + r["role"] + ")")
                lines = []
                for (day, shift), people in sorted(grouped.items()):
                    lines.append(day + " " + shift + ": " + ", ".join(people))
                reply = chr(10).join(lines)

        return jsonify({
            "reply":          reply,
            "schedule":       ui_data.get("schedule", {}),
            "solverData":     solver_data,
            "solverSchedule": solver_sched,
        })

    # --- Handle UI-mutation types (no solver rerun via update_schedule) ---
    change_type = change.get("type", "")

    if change_type == "set_staffing_override":
        # Write per-day or global staffing rule back into ui_data, then re-solve
        day_name  = change.get("day")        # full weekday or null
        shift_name= change.get("shift","")
        role      = change.get("role","")
        count     = int(change.get("count", 0))

        # Find shift id by name (case-insensitive)
        shifts = ui_data.get("shifts", [])
        sh = next((s for s in shifts if s["name"].lower() == shift_name.lower()), None)
        if not sh:
            return jsonify({"reply": f"Shift '{shift_name}' not found.", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched})

        staffing = ui_data.setdefault("staffing", {})
        DAYS_FULL = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        WEEKEND   = {5, 6}

        if day_name and day_name in DAYS_FULL:
            di = DAYS_FULL.index(day_name)
            key = f"{sh['id']}_{role}_d{di}"
            staffing[key] = count
            reply_txt = f"Set {day_name} {shift_name} {role} to {count}."
        else:
            # Apply to both wd and we global rules
            staffing[f"{sh['id']}_{role}_wd"] = count
            staffing[f"{sh['id']}_{role}_we"] = count
            reply_txt = f"Set {shift_name} {role} to {count} for all days."

        # Re-run solver with updated ui_data
        try:
            new_solver_data  = ui_to_solver(ui_data, week_offset)
            new_sched        = generate_schedule(new_solver_data)
        except ValueError as exc:
            return jsonify({"reply": f"Staffing updated but solver failed: {exc}", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched})

        _solver_data     = new_solver_data
        _solver_schedule = new_sched
        new_cells = solver_to_ui(new_sched, ui_data, week_offset)
        patched   = merge_schedule(ui_data.get("schedule", {}), new_cells, week_offset)

        return jsonify({
            "reply":            reply_txt + " Schedule regenerated.",
            "schedule":         patched,
            "solverData":       new_solver_data,
            "solverSchedule":   new_sched,
            "uiDataPatch":      {"staffing": staffing},
        })

    if change_type == "set_day_closed":
        date_str = change.get("date","")
        closed   = bool(change.get("closed", True))
        if not date_str:
            return jsonify({"reply": "No date provided.", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched})

        special = ui_data.setdefault("specialDates", {})
        if closed:
            special[date_str] = {"closed": True, "disabledShifts": []}
            reply_txt = f"{date_str} marked as closed."
        else:
            if date_str in special:
                del special[date_str]
            reply_txt = f"{date_str} reopened."

        # Re-run solver (closed date removes requirements for that day)
        try:
            new_solver_data  = ui_to_solver(ui_data, week_offset)
            new_sched        = generate_schedule(new_solver_data)
        except ValueError as exc:
            return jsonify({"reply": reply_txt + f" (Solver skipped: {exc})", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched, "uiDataPatch": {"specialDates": special}})

        _solver_data     = new_solver_data
        _solver_schedule = new_sched
        new_cells = solver_to_ui(new_sched, ui_data, week_offset)
        patched   = merge_schedule(ui_data.get("schedule", {}), new_cells, week_offset)

        return jsonify({
            "reply":          reply_txt + " Schedule regenerated.",
            "schedule":       patched,
            "solverData":     new_solver_data,
            "solverSchedule": new_sched,
            "uiDataPatch":    {"specialDates": special},
        })

    if change_type == "set_shift_disabled":
        di       = int(change.get("day_index", 0))
        shift_nm = change.get("shift","")
        disabled = bool(change.get("disabled", True))

        hours = ui_data.setdefault("restaurant", {}).setdefault("hours", {})
        dh    = hours.setdefault(str(di), {"open":"09:00","close":"22:00","closed":False,"disabledShifts":[]})
        if "disabledShifts" not in dh:
            dh["disabledShifts"] = []

        existing_ds = [s for s in dh["disabledShifts"] if s.lower() != shift_nm.lower()]
        if disabled:
            existing_ds.append(shift_nm)
        dh["disabledShifts"] = existing_ds

        DAYS_FULL = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        day_name  = DAYS_FULL[di] if 0 <= di <= 6 else str(di)
        action    = "disabled" if disabled else "re-enabled"
        reply_txt = f"{shift_nm} on {day_name}s {action}."

        try:
            new_solver_data  = ui_to_solver(ui_data, week_offset)
            new_sched        = generate_schedule(new_solver_data)
        except ValueError as exc:
            return jsonify({"reply": reply_txt + f" (Solver skipped: {exc})", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched, "uiDataPatch": {"restaurant": ui_data.get("restaurant",{})}})

        _solver_data     = new_solver_data
        _solver_schedule = new_sched
        new_cells = solver_to_ui(new_sched, ui_data, week_offset)
        patched   = merge_schedule(ui_data.get("schedule", {}), new_cells, week_offset)

        return jsonify({
            "reply":          reply_txt + " Schedule regenerated.",
            "schedule":       patched,
            "solverData":     new_solver_data,
            "solverSchedule": new_sched,
            "uiDataPatch":    {"restaurant": ui_data.get("restaurant",{})},
        })

    if change_type == "set_date_staffing":
        date_str  = change.get("date", "")
        shift_nm  = change.get("shift", "")
        role      = change.get("role", "")
        count     = int(change.get("count", 0))

        if not date_str:
            return jsonify({"reply": "No date provided.", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched})

        # Store as a special date with per-shift-role staffing overrides
        special = ui_data.setdefault("specialDates", {})
        if date_str not in special:
            special[date_str] = {"closed": False, "disabledShifts": [], "staffingOverrides": {}}
        if "staffingOverrides" not in special[date_str]:
            special[date_str]["staffingOverrides"] = {}

        override_key = shift_nm + "|" + role
        special[date_str]["staffingOverrides"][override_key] = count

        shifts = ui_data.get("shifts", [])
        sh = next((s for s in shifts if s["name"].lower() == shift_nm.lower()), None)
        if not sh:
            return jsonify({"reply": "Shift " + shift_nm + " not found.", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched})

        reply_txt = "Set " + shift_nm + " " + role + " to " + str(count) + " on " + date_str + "."

        try:
            new_solver_data = ui_to_solver(ui_data, week_offset)
            new_sched_gen   = generate_schedule(new_solver_data)
        except ValueError as exc:
            return jsonify({"reply": reply_txt + " (Solver skipped: " + str(exc) + ")", "schedule": ui_data.get("schedule",{}), "solverData": solver_data, "solverSchedule": solver_sched, "uiDataPatch": {"specialDates": special}})

        _solver_data     = new_solver_data
        _solver_schedule = new_sched_gen
        new_cells = solver_to_ui(new_sched_gen, ui_data, week_offset)
        patched   = merge_schedule(ui_data.get("schedule", {}), new_cells, week_offset)

        return jsonify({
            "reply":          reply_txt + " Schedule regenerated.",
            "schedule":       patched,
            "solverData":     new_solver_data,
            "solverSchedule": new_sched_gen,
            "uiDataPatch":    {"specialDates": special},
        })

    # --- Apply the change via the solver ---
    try:
        new_sched, new_data = update_schedule(solver_data, solver_sched, change)
    except ValueError as exc:
        return jsonify({
            "reply":         "Could not apply change: " + str(exc),
            "schedule":      ui_data.get("schedule", {}),
            "solverData":    solver_data,
            "solverSchedule": solver_sched,
        })

    # --- Generate a human-readable explanation ---
    explanation = generate_explanation(solver_data, solver_sched, new_sched, change)

    # Debug: print what changed so we can verify the solver worked correctly
    old_set = {(r["employee_id"], r["day"], r["shift"]) for r in solver_sched}
    new_set = {(r["employee_id"], r["day"], r["shift"]) for r in new_sched}
    removed = old_set - new_set
    added   = new_set - old_set
    print(f"[DEBUG] Change applied: {change.get('type')} for {change.get('employee_name','?')}")
    print(f"[DEBUG] Parsed change dict: {change}")
    print(f"[DEBUG] Removed assignments: {sorted(removed)}")
    print(f"[DEBUG] Added assignments:   {sorted(added)}")

    # Debug: check Cindy's availability in new_data after the change
    emp_name = change.get("employee_name", "")
    emp_avail = [
        r for r in new_data["availability"]
        if r["employee_id"] in {s["id"] for s in new_data["staff"] if s["name"].lower() == emp_name.lower()}
    ]
    wed_avail = [r for r in emp_avail if r["day"].lower() == "wednesday"]
    print(f"[DEBUG] {emp_name} Wednesday availability in new_data: {wed_avail}")
    
    # Debug: check avail_lookup for this employee on Wednesday
    from with_llm import make_availability_lookup
    avail_lookup = make_availability_lookup(new_data["availability"])
    emp_id = next((s["id"] for s in new_data["staff"] if s["name"].lower() == emp_name.lower()), None)
    if emp_id:
        wed_lookup = {k: v for k, v in avail_lookup.items() if k[0] == emp_id and "wednesday" in k[1].lower()}
        print(f"[DEBUG] avail_lookup for {emp_name} on Wednesday: {wed_lookup}")

    # --- Save updated state ---
    _solver_data     = new_data
    _solver_schedule = new_sched

    # --- Convert back to UI format ---
    new_cells = solver_to_ui(new_sched, ui_data, week_offset)
    print(f"[DEBUG] new_cells keys: {list(new_cells.keys())[:10]}")
    print(f"[DEBUG] week_offset: {week_offset}")
    existing  = ui_data.get("schedule", {})
    patched   = merge_schedule(existing, new_cells, week_offset)
    print(f"[DEBUG] patched keys sample: {list(patched.keys())[:10]}")

    return jsonify({
        "reply":          explanation,
        "schedule":       patched,
        "solverData":     new_data,
        "solverSchedule": new_sched,
        # Echo the parsed change back so the frontend can prompt the user
        # to update D.employees availability for "unavailable" changes.
        "parsedChange":   change,
    })


@app.route("/api/state", methods=["GET"])
def state():
    """Health-check — lets the UI know whether a solver state exists."""
    return jsonify({
        "hasSolverData":     _solver_data is not None,
        "assignmentCount":   len(_solver_schedule) if _solver_schedule else 0,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"ShiftWhisk backend starting on http://localhost:{port}")
    print("Make sure GROQ_API_KEY is set in your environment.")
    app.run(host="0.0.0.0", port=port, debug=debug)
