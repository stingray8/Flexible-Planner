from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


PALETTE = ["#3B82F6", "#10B981", "#8B5CF6", "#F97316", "#EC4899", "#06B6D4"]


def week_start(day: date) -> date:
    """Return the Sunday that starts the user's planning week."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def initial_state(today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    week = week_start(today)
    course_ids = ["cs101", "calc", "physics"]
    courses = [
        {"id": "cs101", "name": "CS 101", "color": PALETTE[0], "difficulty": 5, "default_target": 10.0, "completed_hours": {}},
        {"id": "calc", "name": "Calculus", "color": PALETTE[1], "difficulty": 3, "default_target": 6.0, "completed_hours": {}},
        {"id": "physics", "name": "Physics", "color": PALETTE[2], "difficulty": 4, "default_target": 8.0, "completed_hours": {}},
    ]
    return {
        "schema_version": "1.0",
        "meta": {"created_at": iso_now(), "updated_at": iso_now(), "active_week": week.isoformat(), "last_sync": None},
        "settings": {
            "sleep_hours": 8.5, "bedtime": "01:00", "bedtime_prep_minutes": 45,
            "wake_buffer_minutes": 30, "rest_hours": 2.0, "transition_minutes": 10,
            "lunch_window": ["12:00", "14:00"], "dinner_window": ["18:00", "21:00"],
            "workout_window": ["13:00", "15:30"], "workout_minutes": 45,
            "lunch_minutes": 45, "dinner_minutes": 60,
            "deep_work_minutes": 90, "day_start": "09:00", "day_end": "01:00",
            "show_holidays": True, "selected_strategy": "balanced",
        },
        "courses": courses,
        "weekly_overrides": {},
        "calendar_feeds": [],
        "events": [],
        "event_overrides": {},
        "routine_overrides": {},
        "tasks": [
            {"id": "task-room", "title": "Weekly room cleaning", "course_id": None, "constraint_type": "flexible", "start": None, "end": None, "deadline": None, "duration_minutes": 45, "severity": 2, "difficulty": 2, "recurrence": "weekly", "completed": False, "notes": ""},
            {"id": "task-review", "title": "Review upcoming assignments", "course_id": course_ids[0], "constraint_type": "flexible", "start": None, "end": None, "deadline": (week + timedelta(days=4, hours=0)).isoformat() + "T20:00:00", "duration_minutes": 60, "severity": 4, "difficulty": 3, "recurrence": "none", "completed": False, "notes": ""},
        ],
        "schedule": [],
        "handoffs": {},
        "loss_report": [],
        "selected_candidate": "balanced",
    }


class Store:
    def __init__(self, path: str | Path | None = None):
        root = Path(os.environ.get("FLEXISTUDY_DATA_DIR", Path.home() / ".flexistudy-local"))
        self.path = Path(path) if path else root / "state.json"
        self._lock = threading.RLock()
        self._history: list[dict[str, Any]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(initial_state())

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                backup = self.path.with_suffix(".corrupt.json")
                if self.path.exists():
                    self.path.replace(backup)
                data = initial_state()
                self.save(data)
            if data.get("schema_version") != "1.0":
                raise ValueError("Unsupported state schema; expected 1.0")
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            data = copy.deepcopy(data)
            data.setdefault("meta", {})["updated_at"] = iso_now()
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(temp, self.path)

    def mutate(self, fn, record_history: bool = True):
        with self._lock:
            state = self.load()
            before = copy.deepcopy(state)
            result = fn(state)
            if record_history and state != before:
                self._history.append(before)
                self._history = self._history[-50:]
            self.save(state)
            return result if result is not None else state

    def undo(self) -> dict[str, Any]:
        with self._lock:
            if not self._history:
                raise ValueError("Nothing to undo")
            state = self._history.pop()
            self.save(state)
            return state

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:10]}"

    def rollover(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        current = week_start(today)

        def apply(state):
            prior = date.fromisoformat(state["meta"].get("active_week", current.isoformat()))
            if current <= prior:
                return state
            state["meta"]["active_week"] = current.isoformat()
            cutoff = current - timedelta(days=14)
            state["schedule"] = [x for x in state.get("schedule", []) if datetime.fromisoformat(x["start"]).date() >= cutoff]
            state["weekly_overrides"] = {k: v for k, v in state.get("weekly_overrides", {}).items() if date.fromisoformat(k) >= current}
            return state

        return self.mutate(apply, record_history=False)

