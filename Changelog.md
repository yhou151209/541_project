# ShiftWhisk — CHANGELOG

### Architecture
```
index.html   → Frontend UI (vanilla JS, no framework)
app.py       → Flask backend (3 REST endpoints)
adapter.py   → Converts UI data ↔ solver format
with_llm.py  → OR-Tools CP-SAT solver + Groq LLM parser
```

### How to Run
```bash
# Terminal 1 — backend
GROQ_API_KEY=your-key python app.py

# Terminal 2 — frontend
python -m http.server 3000
```
Open `http://localhost:3000`

---

## Session 1

### Core Integration
- Built `adapter.py` — bridges the UI's `D` object to the solver's JSON schema
- Built `app.py` — three endpoints:
  - `POST /api/schedule/generate` — Auto Schedule
  - `POST /api/chat` — LLM + solver chat
  - `GET /api/state` — health check
- Wired `index.html` to the real backend (replaced all mock responses)

### Employee ID
- Auto-generated 8-char alphanumeric ID (e.g. `A3F9B2C1`) on every employee
- Displayed as read-only badge on employee cards and edit modal
- Used by solver as canonical identifier to handle duplicate names
- Supported in chat: `"A3F9B2C1 can't work Monday"`

### Auto Schedule Button
- Added `✦ Auto Schedule` button to the schedule page
- Calls the solver directly — fills the entire week in one click
- Chat window only needed for adjustments after auto-schedule runs

### CSV Import
- Added `Skills` column (pipe-separated): `Cashier|Cook`
- Added `Availability` column: `Mon-Morning|Tue-Evening` or `All`
- Role and shift matching is case-insensitive

### Case-Insensitive Matching
- `with_llm.py` — skill check, availability lookup, name matching
- `adapter.py` — roles and skills normalised to lowercase throughout
- `index.html` CSV import — role matching, skill matching

---

## Session 2

### Teammate Solver Improvements (merged from notebook)
- `DAY_ALIASES` + `SHIFT_ALIASES` — `mon/tue/am/pm/night` etc. all supported
- `simplify_text()` + `resolve_with_aliases()` — unified text normalisation
- `resolve_employee_name_fuzzy()` — typo correction via difflib (`Iann → Ian`)
- Cross-day back-to-back — evening → next morning also counts as back-to-back
- `shift_preference` supports negative penalty — negative = prefer, positive = avoid
- `set_availability_by_pattern()` — batch set availability for whole day / shift type
- `validate_data()` auto-normalises day/shift on load

### New Scheduling Priorities (in order)
1. Hard constraints — availability, skills, staffing counts, max hours
2. Full-time first — employees with `max_hours >= 30` get filled first (weight 6)
3. Seniority on busy shifts — evening/weekend penalises low-seniority staff
4. Senior coverage — soft penalty when a shift has no senior (seniority >= 2) available
5. Fairness — minimise max load across part-time employees after full-timers are satisfied
6. Stability — keep previous assignments when re-optimising
7. User preferences — `shift_preference`, `avoid_back_to_back`

### LLM Improvements
- Employee lookup supports 3 methods: ID / name / shift+role slot lookup
- No shift specified → marks all shifts that day unavailable
- Shift name matching is fuzzy: `"morning"` matches `"Morning Shift"`
- Fixed `direct_swap` employee resolution (suffix key bug `_1`/`_2`)
- Updated prompt with examples for all change types including negative penalty

### Preferences Panel
- `Preferences` button on schedule page shows accumulated preferences
- Lists each preference with penalty badge (green = prefer, orange = avoid)
- Each preference can be individually deleted with Remove button
- Deleting a preference prompts user to re-run Auto Schedule

### Preferences Persistence
- Solver state (including preferences) saved to `D.solverCache` in localStorage
- Survives page refresh — preferences not lost on reload
- Auto Schedule always loads persisted preferences before re-running solver

### Export CSV
- `Export CSV` button on schedule page
- Downloads current week's schedule as a CSV file
- Format: Week, Day, Date, Shift, Role, Employee, Employee ID
- Unfilled slots marked as `(unfilled)`
- Filename: `schedule_YYYY-MM-DD.csv`

### Availability Sync (chat to employee panel)
- After an `unavailable` chat change, bot asks if user wants to permanently update availability
- Two buttons: Yes update / No keep as is
- Yes updates `D.employees` availability and saves to localStorage
- Only triggered for `unavailable` type — swap and preferences not affected
- Handles empty availability array correctly (expand all slots first, then remove)

### Bug Fixes
- Fixed custom shift names (e.g. `Afternoon`) being incorrectly mapped to `morning`
- `normalize_shift()` now falls back gracefully for unrecognised shift names
- `solver_to_ui()` shift name matching now case-insensitive
- `set_availability_by_pattern()` correctly handles all shift types including custom names
- `direct_swap` resolve employee fixed for `_1`/`_2` suffix keys

---

## Session 3

### Per-Day Shift Configuration
- Operating Hours page: each day now shows shift pills (Morning / Afternoon / Evening)
- Click a pill to toggle that shift off for that weekday — affects every week going forward
- Disabled shifts show as "Closed" in the schedule grid (same visual as a closed day)
- Stored in `D.restaurant.hours[di].disabledShifts` — array of shift names
- `isShiftDisabled(di, shiftId)` helper checks both global hours and special date overrides
- `adapter.py` filters disabled shifts out of availability and shift_requirements so the solver never sees them
- Manual assignment blocked on disabled shift slots

### Special Date Overrides
- New section at the bottom of Operating Hours: "Special Date Overrides"
- Manager picks a date + type: "Closed all day" or "Disable specific shifts"
- Stored in `D.specialDates["YYYY-MM-DD"]` — takes priority over weekly schedule
- Closed special dates show as "Closed" in schedule; disabled-shift dates show "Closed" on affected cells
- ★ marker appears in schedule column header when a special date override is active
- Individual overrides can be removed from the list
- `adapter.py` checks `specialDates` before falling back to `restaurant.hours`

### New LLM Change Types (9 total, up from 4)
- `remove_from_shift` — "Remove Alice from Tuesday morning" — marks unavailable for that slot, re-solves
- `schedule_query` — "Who's working Saturday evening?" — answered client-side instantly, no solver call
- `set_staffing_override` — "Sunday Morning only needs 1 Server" — writes per-day staffing key, re-runs Auto Schedule
- `set_day_closed` — "Close Christmas day" / "Next Wednesday is closed" — writes to `specialDates`, re-runs solver
- `set_shift_disabled` — "Don't open Morning on Sundays" — writes to `hours[di].disabledShifts`, re-runs solver
- Today's date injected into every chat message so LLM can resolve relative dates ("next Wednesday")
- `app.py` returns `uiDataPatch` for UI-mutation types; frontend applies patch and saves to localStorage

### Undo Auto Schedule
- Every Auto Schedule run saves the previous state to `undoStack` (capped at 5)
- "↩ Undo" button appears next to Auto Schedule after first run
- Restores schedule + solver state to the previous snapshot

### Preference History
- Each new preference stamped with `addedAt` ISO timestamp when received from backend
- Preferences panel shows "Added Mar 15 02:30 PM" under each preference entry

### Chat UX
- Auto Schedule no longer sends a message to chat on completion
- First time the chat window is opened, shows: "Hi! What can I help you with today?"
- Local schedule query interception — simple "who's working" queries answered instantly without backend call

### Staffing Rules — Per-Day Override UI
- Each shift card has a "▸ Per-day overrides" expandable section
- Shows a Role × Mon–Sun table; empty cell = use global weekday/weekend default
- Filled cells shown with purple border to indicate override is active
- `neededForRole()` checks per-day key first, falls back to `wd`/`we`
- Changes save immediately and re-render the schedule

---

## Architectural Decision Log

### Staffing model: weekday/weekend → per-day (planned)
**Current state:** Two-tier system — global `_wd`/`_we` keys with optional `_d0`–`_d6` per-day overrides. The two systems co-existing has caused bugs (solver not picking up per-day overrides correctly, priority conflicts in `adapter.py`).

**Planned change:** Collapse to a single `_d0`–`_d6` key per (shift, role, day). No more `_wd`/`_we`.

**Migration plan:**
- `normalizeLegacyData` in `index.html` expands existing `_wd`/`_we` keys into 7 day keys on first load (weekday value → `_d1`–`_d5`, weekend value → `_d0`, `_d6`)
- Setup Step 3 keeps weekday/weekend UI as a **quick-fill shortcut** that writes all 7 day keys at once
- `adapter.py` `shift_requirements` loop reads only `_d0`–`_d6`, removing the suffix branch entirely
- `neededForRole()` in `index.html` reads only `_d0`–`_d6`
- All chat commands (`set_staffing_override`) write directly to the day key

**Why:** Simpler mental model for managers, eliminates the override priority bug, cleaner adapter code.

---

# Known Limitations

## Scheduling
- **No event-based staffing boost** — can't temporarily increase headcount for a specific date (e.g. New Year's Eve needs 5 Servers instead of 3); workaround is to manually edit Staffing Rules
- **Solver timeout 15s** — large rosters may return a suboptimal schedule
- **Per-day staffing override bug** — solver may not pick up per-day overrides correctly until the weekday/weekend → per-day migration is complete (see Architectural Decision Log)

## LLM
- **No conversation memory** — each chat message is independent; context doesn't carry between messages
- **Groq rate limits** — free tier has request limits; no retry logic

## Data
- **localStorage only** — clearing browser data or switching computers loses everything
- **No PDF export** — CSV only; no print-friendly view

---

# Backlog (next to build)

## High Priority
- **Staffing model migration** — collapse `_wd`/`_we` + `_dN` override into single `_d0`–`_d6` system (see Architectural Decision Log)
- **Multi-device sync** — replace localStorage with a real database (Firebase recommended)

## Medium Priority
- **Event staffing boost** — chat command to temporarily increase headcount on a specific date
- **Employee self-service** — employees view their own schedule and set availability
- **Notifications** — notify employees when schedule is published or changed

## Low Priority
- **Multi-manager support** — role-based access control
