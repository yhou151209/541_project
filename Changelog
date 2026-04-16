# ShiftWhisk — Dev Notes

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

## Changes Made Today

### Core Integration
- Built `adapter.py` — bridges the UI's `D` object to the solver's JSON schema
- Built `app.py` — three endpoints:
  - `POST /api/schedule/generate` — Auto Schedule
  - `POST /api/chat` — LLM + solver chat
  - `GET /api/state` — health check
- Wired `index.html` to the real backend (replaced all mock responses)

### Employee ID
- Auto-generated 8-char alphanumeric ID (`A3F9B2C1`) on every employee
- Displayed as a read-only badge on employee cards and edit modal
- Used by the solver as the canonical identifier to handle duplicate names
- Supported in chat: `"A3F9B2C1 can't work Monday"`

### Auto Schedule Button
- Added `Auto Schedule` button to the schedule page
- Calls the solver directly — fills the entire week in one click
- Chat window is only needed for adjustments after auto-schedule runs

### LLM Improvements
- Employee lookup now supports 3 methods: ID / name / shift+role slot
- `"Julia can't work Monday"` (no shift specified) → marks all shifts on that day unavailable
- Shift name matching is now fuzzy: `"morning"` matches `"Morning Shift"`
- Fixed `direct_swap` employee resolution (suffix key bug `_1`/`_2`)

### Case-Insensitive Matching
- `with_llm.py` — skill check, availability lookup, name matching
- `adapter.py` — roles and skills normalised to lowercase throughout
- `index.html` CSV import — role matching, skill matching

### CSV Import
- Added `Skills` column (pipe-separated): `Cashier|Cook`
- Added `Availability` column: `Mon-Morning|Tue-Evening` or `All`
- Role matching is now case-insensitive (`server` matches `Server`)
- Employee ID auto-generated on import

---

## Known Limitations & Things to Improve

### LLM
- **Too many unsupported requests** — the solver only handles 4 change types
  (`unavailable`, `direct_swap`, `avoid_back_to_back`, `avoid_shift`).
  Common requests like "add a shift", "remove someone from Tuesday", or
  "who is working Saturday?" are not supported yet.
- **No conversation memory** — every chat message is independent.
  The LLM doesn't remember what was said earlier in the chat window.

### Solver
- **No hard constraint for "must work" rules** — can only say unavailable,
  not "must be assigned to this shift".
- **No multi-week memory** — solver reruns from scratch each time.
  Preferences set via chat are lost on page refresh.
- **Solver timeout is 10s** — large staff rosters may hit the limit and
  return a suboptimal schedule.

### UI / Data
- **Data lives in localStorage** — clearing browser data wipes everything.
  No real database or user accounts backend.
- **No export** — can't export the final schedule to PDF, Excel, or print view.
- **No notifications** — no way to notify employees of their schedule.
- **Single manager** — no multi-user or role-based access (e.g. employee
  self-service to set their own availability).
- **No undo** — once Auto Schedule runs it overwrites the current week.
  No history or rollback.

### Deployment
- Currently local only — needs a production WSGI server (e.g. Gunicorn)
  and a hosting platform (e.g. Railway) to go live.
- `index.html` is a static file — needs to be served separately
  (e.g. GitHub Pages / Netlify) for public access.
