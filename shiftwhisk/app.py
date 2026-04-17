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
    try:
        change = parse_user_request(message, solver_data, solver_sched)
    except (ValueError, RuntimeError) as exc:
        # Return a friendly bot reply rather than an HTTP error so the
        # chat window can display it directly.
        return jsonify({
            "reply":         str(exc),
            "schedule":      ui_data.get("schedule", {}),
            "solverData":    solver_data,
            "solverSchedule": solver_sched,
        })

    # --- Apply the change via the solver ---
    try:
        new_sched, new_data = update_schedule(solver_data, solver_sched, change)
    except ValueError as exc:
        return jsonify({
            "reply":         f"Could not apply change: {exc}",
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
