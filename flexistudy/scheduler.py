from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

SLOT_MINUTES = 15
SLOTS_PER_DAY = 96
STRATEGIES = {
    "targets": {"study": 1.35, "routine": .75, "late": 1.0, "split": False, "weekend": 1.0},
    "balanced": {"study": 1.0, "routine": 1.4, "late": 1.0, "split": False, "weekend": 1.0},
    "night": {"study": 1.15, "routine": .9, "late": .2, "split": False, "weekend": 1.0},
    "split": {"study": 1.0, "routine": 1.0, "late": 1.0, "split": True, "weekend": 1.0},
    "weekend": {"study": 1.05, "routine": 1.0, "late": 1.0, "split": False, "weekend": .25},
}


def parse_hhmm(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def week_start_for(state: dict[str, Any], requested: str | None = None) -> date:
    return date.fromisoformat(requested or state["meta"]["active_week"])


def target_for(state: dict[str, Any], course: dict[str, Any], week: date) -> float:
    return float(state.get("weekly_overrides", {}).get(week.isoformat(), {}).get(course["id"], course["default_target"]))


def course_label(course: dict[str, Any]) -> str:
    key = course.get("course_key", "")
    if key.startswith("catalog:"):
        number = key.split(":", 1)[1]
        return f"{number[:2]}-{number[2:]}" if len(number) == 5 else number
    return course["name"]


def _slot_index(origin: datetime, value: datetime) -> int:
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    if origin.tzinfo is not None:
        origin = origin.astimezone().replace(tzinfo=None)
    return math.floor((value - origin).total_seconds() / (SLOT_MINUTES * 60))


def _occupied(state: dict[str, Any], origin: datetime, total_slots: int) -> np.ndarray:
    used = np.zeros(total_slots, dtype=bool)
    hard_items = [e for e in state.get("events", []) if e.get("enforcement") == "hard"]
    hard_items += [t for t in state.get("tasks", []) if t.get("constraint_type") == "fixed" and t.get("start") and not t.get("completed")]
    hard_items += [x for x in state.get("schedule", []) if (x.get("locked") or x.get("completed")) and x.get("start")]
    for item in hard_items:
        try:
            start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
            transition_minutes = int(state["settings"].get("transition_minutes", 0))
            local_start = start.astimezone().replace(tzinfo=None) if start.tzinfo else start
            local_end = end.astimezone().replace(tzinfo=None) if end.tzinfo else end
            a = max(0, math.floor(((local_start - timedelta(minutes=transition_minutes)) - origin).total_seconds() / 900))
            b = min(total_slots, math.ceil(((local_end + timedelta(minutes=transition_minutes)) - origin).total_seconds() / 900))
            if b > a:
                used[a:b] = True
        except (ValueError, TypeError, KeyError):
            continue
    settings = state["settings"]
    sleep_minutes = int(float(settings["sleep_hours"]) * 60 + int(settings["bedtime_prep_minutes"]))
    bedtime = parse_hhmm(settings["bedtime"])
    wake = (bedtime + sleep_minutes) % 1440
    for day in range(14):
        base = day * SLOTS_PER_DAY
        for slot in range(SLOTS_PER_DAY):
            minute = slot * SLOT_MINUTES
            asleep = (bedtime <= minute < wake) if bedtime < wake else (minute >= bedtime or minute < wake)
            if asleep:
                used[base + slot] = True
    return used


def _place_routines(state: dict[str, Any], origin: datetime, used: np.ndarray) -> tuple[list[dict[str, Any]], list[str]]:
    settings = state["settings"]
    definitions = [
        ("lunch", "Lunch", settings["lunch_window"], int(settings.get("lunch_minutes", 45)), "LUNCH"),
        ("workout", "Exercise", settings["workout_window"], int(settings["workout_minutes"]), "WORKOUT"),
        ("dinner", "Dinner", settings["dinner_window"], int(settings.get("dinner_minutes", 60)), "DINNER"),
    ]
    transition = math.ceil(int(settings.get("transition_minutes", 0)) / SLOT_MINUTES)
    plan: list[dict[str, Any]] = []
    warnings: list[str] = []
    preserved_ids = {item["id"] for item in state.get("schedule", []) if item.get("kind") == "routine" and (item.get("locked") or item.get("completed"))}
    for day in range(14):
        day_date = origin.date() + timedelta(days=day)
        base = day * SLOTS_PER_DAY
        for routine_id, title, window, minutes, source in sorted(definitions, key=lambda item: parse_hhmm(item[2][0])):
            item_id = f"routine-{routine_id}-{day_date.isoformat()}"
            if item_id in preserved_ids:
                continue
            length = math.ceil(minutes / SLOT_MINUTES)
            override = state.get("routine_overrides", {}).get(item_id)
            if override:
                start_dt = datetime.fromisoformat(override["start"])
                end_dt = datetime.fromisoformat(override["end"])
                start_slot = base + (start_dt.hour * 60 + start_dt.minute) // SLOT_MINUTES
                length = max(1, math.ceil((end_dt - start_dt).total_seconds() / (SLOT_MINUTES * 60)))
            else:
                window_start, window_end = parse_hhmm(window[0]), parse_hhmm(window[1])
                preferred = range(base + window_start // SLOT_MINUTES, base + (window_end - minutes) // SLOT_MINUTES + 1)
                # Existing hard events already carry their travel buffer. Check
                # the routine's actual span here, then reserve its own buffer.
                start_slot = next((slot for slot in preferred if not used[slot:slot+length].any()), -1)
                if start_slot < 0:
                    # Keep protected routines near their preferred window when a
                    # class fills the whole window; never send lunch hours away.
                    nearby_start = max(base + 8 * 4, base + (window_start - 90) // SLOT_MINUTES)
                    nearby_end = min(base + 23 * 4 - length, base + (window_end + 90 - minutes) // SLOT_MINUTES)
                    nearby = sorted(
                        range(nearby_start, nearby_end + 1),
                        key=lambda slot: (max(0, base + window_start // SLOT_MINUTES - slot, slot + length - (base + window_end // SLOT_MINUTES)), abs(slot - (base + window_start // SLOT_MINUTES))),
                    )
                    start_slot = next((slot for slot in nearby if not used[slot:slot+length].any()), -1)
                if start_slot < 0:
                    warnings.append(f"No conflict-free time near the {title.lower()} window was available on {day_date.strftime('%A, %b %-d')}.")
                    continue
                start_dt = origin + timedelta(minutes=start_slot * SLOT_MINUTES)
                end_dt = start_dt + timedelta(minutes=minutes)
            used[max(base, start_slot-transition):min(base+SLOTS_PER_DAY, start_slot+length+transition)] = True
            plan.append({"id": item_id, "kind": "routine", "owner_id": item_id, "title": title,
                "start": start_dt.isoformat(timespec="minutes"), "end": end_dt.isoformat(timespec="minutes"),
                "completed": False, "locked": False, "source": source, "strategy": "protected-routine"})
    return plan, warnings


@dataclass
class Candidate:
    kind: str
    owner_id: str
    title: str
    start_slot: int
    length: int
    score: float
    severity: int = 0
    notes: str = ""
    course_id: str | None = None


def solve(state: dict[str, Any], strategy_name: str = "balanced", start_week: str | None = None) -> dict[str, Any]:
    strategy = STRATEGIES.get(strategy_name, STRATEGIES["balanced"])
    week = week_start_for(state, start_week)
    # The grid represents wall-clock local time. Normalize imported aware iCal
    # datetimes at the boundary so they can coexist with datetime-local tasks.
    origin = datetime.combine(week, time.min)
    total_slots = 14 * SLOTS_PER_DAY
    occupied = _occupied(state, origin, total_slots)
    routine_plan, routine_warnings = _place_routines(state, origin, occupied)
    now = datetime.now().astimezone().replace(tzinfo=None)
    not_before_slot = max(0, math.ceil((now - origin).total_seconds() / (SLOT_MINUTES * 60))) if origin <= now < origin + timedelta(days=14) else 0
    courses = state.get("courses", [])
    candidates: list[Candidate] = []
    deficits: dict[str, float] = {}
    course_targets: dict[str, float] = {}
    block_slots = 3 if strategy["split"] else max(3, int(state["settings"]["deep_work_minutes"]) // SLOT_MINUTES)
    protected_hours: dict[tuple[str, str], float] = {}
    replacement_after: dict[str, int] = {}
    for item in state.get("schedule", []):
        if item.get("kind") != "study" or not item.get("locked") or item.get("completed"):
            continue
        try:
            item_start, item_end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
            item_week = (item_start.date() - timedelta(days=(item_start.weekday() + 1) % 7)).isoformat()
            protected_hours[(item["owner_id"], item_week)] = protected_hours.get((item["owner_id"], item_week), 0) + (item_end-item_start).total_seconds()/3600
            if item.get("replaced_course_id"):
                replacement_after[item["replaced_course_id"]] = max(replacement_after.get(item["replaced_course_id"], 0), math.ceil((item_end-origin).total_seconds()/(SLOT_MINUTES*60)))
        except (KeyError, ValueError, TypeError):
            continue

    for course in courses:
        course_id = course["id"]
        first_target = target_for(state, course, week)
        second_week = week + timedelta(days=7)
        second_target = target_for(state, course, second_week)
        course_targets[course_id] = first_target + second_target
        first_remaining = max(0.0, first_target - float(course.get("completed_hours", {}).get(week.isoformat(), 0)) - protected_hours.get((course_id, week.isoformat()), 0))
        second_remaining = max(0.0, second_target - float(course.get("completed_hours", {}).get(second_week.isoformat(), 0)) - protected_hours.get((course_id, second_week.isoformat()), 0))
        deficits[course_id] = first_remaining + second_remaining
        handoff = state.get("handoffs", {}).get(course_id, "")
        for start in range(0, total_slots - block_slots + 1):
            if start < not_before_slot:
                continue
            if start < replacement_after.get(course_id, 0):
                continue
            day, in_day = divmod(start, SLOTS_PER_DAY)
            if in_day < 8 * 4 or in_day + block_slots > 25 * 4:
                continue
            if start // SLOTS_PER_DAY != (start + block_slots - 1) // SLOTS_PER_DAY or occupied[start:start+block_slots].any():
                continue
            hour = in_day / 4
            late_penalty = max(0, hour - 21) * strategy["late"]
            weekend_penalty = (0 if day % 7 >= 5 else 1) * (1 - strategy["weekend"])
            difficulty_bonus = float(course.get("difficulty", 3)) * (1.5 if 17 <= hour <= 22 else .7)
            score = 100 * strategy["study"] + difficulty_bonus - late_penalty + weekend_penalty
            candidates.append(Candidate("study", course_id, f"Study {course_label(course)}", start, block_slots, score, notes=handoff))

    for task in state.get("tasks", []):
        if task.get("completed") or task.get("assigned_block_id") or task.get("constraint_type") != "flexible":
            continue
        # Course-linked Canvas work belongs inside that course's study sessions,
        # not in a second, visually unrelated generic task block.
        if task.get("course_id") and task.get("feed_id"):
            continue
        length = max(1, math.ceil(int(task.get("duration_minutes", 30)) / SLOT_MINUTES))
        deadline_slot = total_slots
        if task.get("deadline"):
            deadline_slot = min(total_slots, _slot_index(origin, datetime.fromisoformat(task["deadline"])) - (96 if int(task.get("severity", 1)) == 5 else 0))
        for start in range(not_before_slot, max(not_before_slot, deadline_slot - length + 1)):
            day, in_day = divmod(start, SLOTS_PER_DAY)
            if in_day < 9 * 4 or in_day + length > 23 * 4 or occupied[start:start+length].any():
                continue
            urgency = max(0, 14 - (deadline_slot - start) / 96)
            score = 15 + int(task.get("severity", 1)) * 30 + int(task.get("difficulty", 1)) * 2 + urgency
            candidates.append(Candidate("task", task["id"], task["title"], start, length, score, int(task.get("severity", 1)), task.get("notes", ""), task.get("course_id")))

    # Keep the model compact and deterministic: best candidate per owner/start-hour.
    compact: dict[tuple[str, str, int], Candidate] = {}
    for c in candidates:
        # Keep study starts aligned to their own duration. This preserves
        # back-to-back options (for example 9:00–10:30, 10:30–12:00) instead
        # of forcing every block onto an hourly grid.
        key = (c.kind, c.owner_id, c.start_slot // (block_slots if c.kind == "study" else 1))
        if key not in compact or c.score > compact[key].score:
            compact[key] = c
    candidates = sorted(compact.values(), key=lambda c: (c.start_slot, c.owner_id, c.kind))
    n = len(candidates)
    if not n:
        loss = [{"tier": 1, "message": message} for message in routine_warnings]
        if not loss and any(deficits.values()):
            loss.append({"tier": 1, "message": "No schedulable capacity exists in this horizon."})
        if not loss:
            loss.append({"tier": 0, "message": "Protected routines fit and no flexible work remains."})
        return {"schedule": routine_plan, "loss_report": loss, "status": "degraded" if any(item["tier"] for item in loss) else "optimal"}

    course_days = sorted({(c.owner_id, c.start_slot // SLOTS_PER_DAY) for c in candidates if c.kind == "study"})
    day_variable = {key: n + index for index, key in enumerate(course_days)}
    total_variables = n + len(course_days)
    # Opening another day for the same course has a cost. With block quotas
    # already fixed, this concentrates each subject into fewer, longer runs.
    objective = np.concatenate((
        -np.array([c.score + (n-i)*1e-7 for i, c in enumerate(candidates)]),
        np.full(len(course_days), 35.0),
    ))
    rows: list[tuple[list[int], float, float]] = []
    by_slot: list[list[int]] = [[] for _ in range(total_slots)]
    by_owner: dict[tuple[str, str], list[int]] = {}
    for i, c in enumerate(candidates):
        for s in range(c.start_slot, c.start_slot+c.length):
            by_slot[s].append(i)
        by_owner.setdefault((c.kind, c.owner_id), []).append(i)
    for indexes in by_slot:
        if len(indexes) > 1:
            rows.append((indexes, 0, 1))
    # Allocate study block quotas with largest-remainder proportional scaling.
    # Because every course has the same candidate windows, exact quotas also
    # guarantee that a constrained week cannot starve one course entirely.
    free_block_starts = sorted({c.start_slot for c in candidates if c.kind == "study"})
    capacity_blocks = 0
    cursor = -1
    for start in free_block_starts:
        if start >= cursor:
            capacity_blocks += 1
            cursor = start + block_slots
    required_blocks = {cid: math.ceil(hours * 60 / (block_slots * SLOT_MINUTES)) for cid, hours in deficits.items()}
    total_required = sum(required_blocks.values())
    quota_blocks = dict(required_blocks)
    if total_required > capacity_blocks and (total_deficit := sum(deficits.values())):
        raw = {cid: capacity_blocks * deficit / total_deficit for cid, deficit in deficits.items()}
        quota_blocks = {cid: min(required_blocks[cid], int(math.floor(value))) for cid, value in raw.items()}
        remaining = capacity_blocks - sum(quota_blocks.values())
        order = sorted(deficits, key=lambda cid: (-(raw[cid] - math.floor(raw[cid])), cid))
        for cid in order:
            if remaining <= 0: break
            if quota_blocks[cid] < required_blocks[cid]:
                quota_blocks[cid] += 1; remaining -= 1

    for (kind, owner), indexes in by_owner.items():
        if kind == "task":
            rows.append((indexes, 0, 1))
        else:
            quota = quota_blocks.get(owner, 0)
            rows.append((indexes, quota, quota))
    activation_rows = [(i, day_variable[(c.owner_id, c.start_slot // SLOTS_PER_DAY)]) for i, c in enumerate(candidates) if c.kind == "study"]
    matrix = lil_matrix((len(rows) + len(activation_rows), total_variables), dtype=float)
    lower, upper = [], []
    for r, (indexes, lo, hi) in enumerate(rows):
        matrix[r, indexes] = 1
        lower.append(lo); upper.append(hi)
    offset = len(rows)
    for row, (candidate_index, active_day_index) in enumerate(activation_rows, start=offset):
        matrix[row, candidate_index] = 1
        matrix[row, active_day_index] = -1
        lower.append(-np.inf); upper.append(0)
    constraints = LinearConstraint(matrix.tocsr(), np.array(lower), np.array(upper))
    result = milp(objective, integrality=np.ones(total_variables), bounds=Bounds(0, 1), constraints=constraints, options={"time_limit": 12, "mip_rel_gap": 0.0})
    chosen = [c for i, c in enumerate(candidates) if result.x is not None and result.x[i] > .5]

    schedule = list(routine_plan)
    allocated = {c["id"]: 0.0 for c in courses}
    for c in chosen:
        start = origin + timedelta(minutes=c.start_slot*SLOT_MINUTES)
        end = start + timedelta(minutes=c.length*SLOT_MINUTES)
        item = {"id": f"slot-{strategy_name}-{c.kind}-{c.owner_id}-{c.start_slot}", "kind": c.kind, "owner_id": c.owner_id,
                "title": c.title, "start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes"),
                "completed": False, "locked": False, "severity": c.severity, "notes": c.notes, "strategy": strategy_name}
        if c.course_id:
            item["course_id"] = c.course_id
        schedule.append(item)
        if c.kind == "study": allocated[c.owner_id] += c.length * SLOT_MINUTES / 60

    # Present adjacent blocks for one subject as the long session they really
    # are. Hard events and protected routines remain explicit boundaries.
    merged_schedule: list[dict[str, Any]] = []
    for item in sorted(schedule, key=lambda entry: entry["start"]):
        previous = merged_schedule[-1] if merged_schedule else None
        if previous and item["kind"] == previous["kind"] == "study" and item["owner_id"] == previous["owner_id"] and item["start"] == previous["end"]:
            previous["end"] = item["end"]
            previous["id"] = f"{previous['id']}-through-{item['id'].rsplit('-', 1)[-1]}"
        else:
            merged_schedule.append(item)
    schedule = merged_schedule

    canvas_tasks = sorted(
        (task for task in state.get("tasks", []) if task.get("feed_id") and task.get("course_id") and task.get("auto_place", True) and not task.get("completed") and not task.get("assigned_block_id")),
        key=lambda task: (task.get("deadline") or "9999", -int(task.get("severity", 1)), task["id"]),
    )
    for task in canvas_tasks:
        deadline = datetime.fromisoformat(task["deadline"]) if task.get("deadline") else None
        blocks = [item for item in schedule if item["kind"] == "study" and item["owner_id"] == task["course_id"]]
        if deadline is None:
            before_deadline = blocks
        elif deadline.tzinfo is not None:
            local_deadline = deadline.astimezone().replace(tzinfo=None)
            before_deadline = [item for item in blocks if datetime.fromisoformat(item["end"]) <= local_deadline]
        else:
            before_deadline = [item for item in blocks if datetime.fromisoformat(item["end"]) <= deadline]
        choices = before_deadline or blocks
        if choices:
            block = min(choices, key=lambda item: (len(item.get("task_ids", [])), item["start"]))
            block.setdefault("task_ids", []).append(task["id"])

    loss = [{"tier": 1, "message": message} for message in routine_warnings]
    total_deficit = sum(deficits.values())
    total_alloc = sum(allocated.values())
    for course in courses:
        cid = course["id"]
        missing = max(0, deficits[cid] - allocated[cid])
        if missing > .01:
            fair = total_alloc * deficits[cid] / total_deficit if total_deficit else 0
            loss.append({"tier": 4, "course_id": cid, "message": f"{course_label(course)}: scheduled {allocated[cid]:.1f}h of {deficits[cid]:.1f}h remaining; proportional capacity share is {fair:.1f}h."})
    unscheduled_critical = [t for t in state.get("tasks", []) if int(t.get("severity", 1)) == 5 and not t.get("completed") and not any(x["owner_id"] == t["id"] or t["id"] in x.get("task_ids", []) for x in schedule)]
    for task in unscheduled_critical:
        loss.append({"tier": 2, "task_id": task["id"], "message": f"Critical task could not be placed before its protected deadline: {task['title']}."})
    if not loss:
        loss.append({"tier": 0, "message": "All requested academic targets and schedulable tasks fit this horizon."})
    return {"schedule": sorted(schedule, key=lambda x: x["start"]), "loss_report": loss, "status": "optimal" if result.success else "best_feasible", "objective": float(result.fun) if result.fun is not None else None}


def candidate_summaries(state: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "targets": "Target & Assignment Protector", "balanced": "Routine Protector",
        "night": "Window Flexer", "split": "Granular Splitter", "weekend": "Weekend Shift",
    }
    output = []
    for key, label in labels.items():
        result = solve(state, key)
        output.append({"id": key, "label": label, "scheduled": len(result["schedule"]), "losses": len([x for x in result["loss_report"] if x["tier"]]), "result": result})
    return output

