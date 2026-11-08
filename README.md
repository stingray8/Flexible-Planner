# FlexiStudy Local

FlexiStudy Local is a private, local-first two-week study planner. It stores its
state in a human-readable JSON file and uses SciPy's deterministic MILP solver
to place study sessions and flexible tasks around calendar locks and routines.

## Run

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Open <http://127.0.0.1:8765>. By default the app opens a browser automatically.
Use `python3 app.py --no-browser` to suppress that behavior.

Data is stored at `~/.flexistudy-local/state.json`. Override this for testing
with `FLEXISTUDY_DATA_DIR=/path/to/folder`.

## Included

- fixed two-week Monday–Sunday planning horizon with Saturday rollover
- 7×2, This Week, and Next Week views
- per-course defaults and per-week target overrides
- hard/flexible/hidden iCalendar feeds and basic RFC 5545 parsing
- per-calendar Hard/Flexible/Ignore rules for multi-calendar iCal feeds
- deterministic 15-minute MILP scheduling with proportional target fairness
- five scheduling strategies and a transparent loss report
- recurring/fixed/flexible tasks with severity-aware rollover
- completion handoff notes propagated to the next course session
- draggable 24-hour timeline; manual moves persist across refreshes
- historical completed-block logging and removable courses
- configurable sleep, meals, workout, rest, prep, and transition preferences
- local JSON persistence with `schema_version: "1.0"`

The HTTP server binds only to localhost and makes no cloud API calls. Remote
iCalendar URLs are fetched directly by the local process when sync is enabled.

# Flexible-Planner
