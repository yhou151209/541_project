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
    ui_data    = body.get("uiData")
    week_offset = int(body.get("weekOffset", 0))

    if not ui_data:
        return _error("Missing 'uiData' in request body.")

    # Convert UI data → solver format
    try:
        solver_data = ui_to_solver(ui_data, week_offset)
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
    # The UI echoes back the solver state it received from the last call
    client_solver_data  = body.get("solverData")
    client_solver_sched = body.get("solverSchedule")

    if not ui_data:
        return _error("Missing 'uiData' in request body.")
    if not message:
        return _error("Missing 'message' in request body.")

    # Use client-provided solver state if available; fall back to server memory
    solver_data  = client_solver_data  or _solver_data
    solver_sched = client_solver_sched or _solver_schedule

    # If still no solver state, auto-generate before processing the chat
    if solver_data is None or solver_sched is None:
        try:
            solver_data  = ui_to_solver(ui_data, week_offset)
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

    # --- Save updated state ---
    _solver_data     = new_data
    _solver_schedule = new_sched

    # --- Convert back to UI format ---
    new_cells = solver_to_ui(new_sched, ui_data, week_offset)
    existing  = ui_data.get("schedule", {})
    patched   = merge_schedule(existing, new_cells, week_offset)

    return jsonify({
        "reply":          explanation,
        "schedule":       patched,
        "solverData":     new_data,
        "solverSchedule": new_sched,
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
