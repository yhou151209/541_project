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
- Manager picks a date + type: "Closed all day", "Disable specific shifts", or "Boost / reduce staffing"
- Stored in `D.specialDates["YYYY-MM-DD"]` — takes priority over weekly schedule
- Closed special dates show as "Closed" in schedule; disabled-shift dates show "Closed" on affected cells
- ★ marker appears in schedule column header when a special date override is active
- Individual overrides can be removed from the list
- `adapter.py` checks `specialDates` before falling back to `restaurant.hours`

### New LLM Change Types (10 total, up from 4)
- `remove_from_shift` — "Remove Alice from Tuesday morning" — marks unavailable for that slot, re-solves
- `schedule_query` — "Who's working Saturday evening?" — answered client-side instantly, no solver call
- `set_staffing_override` — "Sunday Morning only needs 1 Server" — writes per-day staffing key, re-runs Auto Schedule
- `set_day_closed` — "Close Christmas day" / "Next Wednesday is closed" — writes to `specialDates`, re-runs solver
- `set_shift_disabled` — "Don't open Morning on Sundays" — writes to `hours[di].disabledShifts`, re-runs solver
- `set_date_staffing` — "New Year's Eve needs 5 Servers for Evening" — writes to `specialDates[date].staffingOverrides`, re-runs solver
- Today's date + explicit next-weekday dates injected into every chat message so LLM never miscalculates relative dates
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
- Panel stays open after editing a cell (no full page re-render)
- `neededForRole()` checks per-day key first, falls back to `wd`/`we`

---

## Session 4

### Bug Fixes

**direct_swap "Forced assignment not in model"**
- Root cause: shift and role names in forced assignments were lowercased (`"evening"`, `"server"`) but solver variables use the original casing from `shift_requirements` (`"Evening"`, `"Server"`)
- Fix: added `exact_shift()` and `exact_role()` helpers in `update_schedule` that look up the canonical casing from `shift_requirements` before constructing forced assignments

**"Is Ian working tomorrow?" returns full week**
- Root cause: `findDay()` in `tryLocalScheduleQuery` only matched explicit day names, not relative terms; result was no day filter applied
- Fix: `findDay()` now resolves `today`, `tomorrow`, `yesterday` using current weekday; new `findDayIndex()` helper used in the isWorking branch to correctly filter results to one day

**"Prefer Brian for weekend evening" returns two JSON objects**
- Root cause: LLM interpreted "weekend" as Saturday + Sunday and returned two separate JSON objects, causing `json.loads()` to fail with "Extra data"
- Fix 1: prompt now explicitly instructs LLM to return exactly ONE JSON object and omit `day` for weekend preferences
- Fix 2: added fallback parser in `with_llm.py` — if `json.loads()` fails, extract the first valid JSON object with regex and continue

**"Next Monday is closed" marks wrong date**
- Root cause: LLM was given only today's date and had to compute "next Monday" itself, which it got wrong when today was Saturday
- Fix: `app.py` now pre-computes the exact date of every next weekday and injects them all into the message context, e.g. `next Monday=2026-04-20, next Tuesday=2026-04-21...` — LLM reads the answer directly instead of calculating

**`set_staffing_override` / `set_day_closed` / `set_shift_disabled` / `schedule_query` failing with "Could not identify employee"**
- Root cause: `parse_user_request` called `resolve_employee()` on all change types, including ones that don't reference an employee
- Fix: added `NO_EMPLOYEE_TYPES` set; these types now skip employee resolution entirely

**Per-day staffing override not picked up by solver**
- Root cause: `adapter.py` `shift_requirements` loop only read `_wd`/`_we` keys, ignoring `_dN` per-day overrides
- Fix: loop now checks per-day key first (`_d0`–`_d6`), falls back to global key (`_wd`/`_we`)

---

# Known Limitations

## Scheduling
- **Solver timeout 15s** — large rosters may return a suboptimal schedule

## LLM
- **No conversation memory** — each chat message is independent; context doesn't carry between messages
- **Groq rate limits** — free tier has request limits; no retry logic
- **Social messages trigger schedule_query** — sending "thank you" or similar is parsed as a query and returns the full week schedule

## Data
- **localStorage only** — clearing browser data or switching computers loses everything

---

# Backlog (next to build)

## High Priority
- **Multi-device sync** — replace localStorage with a real database 

## Medium Priority
- **Social message filter** — detect non-scheduling messages and reply with a friendly nudge instead of querying the schedule
- **Employee self-service** — employees view their own schedule and set availability
- **Notifications** — notify employees when schedule is published or changed

## Low Priority
- **Multi-manager support** — role-based access control
