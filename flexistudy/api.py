from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .ical import fetch_feed
from .scheduler import candidate_summaries, course_label, solve
from .store import Store, iso_now


def week_key(value: datetime) -> str:
    day = value.date()
    return (day - timedelta(days=(day.weekday() + 1) % 7)).isoformat()


def class_details(title: str) -> tuple[str, str, str]:
    """Return a stable course key/name and the kind of class meeting."""
    cleaned = title.strip()
    meeting_type = "recitation" if re.match(r"(?i)^rec(?:itation)?\.?\s+", cleaned) else "lab" if re.search(r"(?i)\blab\b", cleaned) else "lecture"
    cleaned = re.sub(r"(?i)^rec(?:itation)?\.?\s+", "", cleaned).strip()
    if cleaned.casefold() == "mse lab":
        return "catalog:27100", "Engineering the Materials of the Future", "lab"
    match = re.match(r"^(.*?)\s*::\s*(\d{5})(?:\s+\S+)?\s*$", cleaned)
    if match:
        name, catalog = match.group(1).strip(), match.group(2)
        return f"catalog:{catalog}", name, meeting_type
    return f"title:{cleaned.casefold()}", cleaned, meeting_type


def course_for_calendar_item(courses: list[dict[str, Any]], title: str, calendar_name: str = "") -> dict[str, Any] | None:
    """Match Canvas-style labels such as 21-127 to imported catalog:21127 courses."""
    haystack = f"{title} {calendar_name}".casefold()
    digits = re.sub(r"\D", "", haystack)
    for course in courses:
        key = course.get("course_key", "")
        if key.startswith("catalog:") and key.split(":", 1)[1] in digits:
            return course
    words = set(re.findall(r"[a-z0-9]+", haystack))
    scored = []
    for course in courses:
        name_words = {word for word in re.findall(r"[a-z0-9]+", course["name"].casefold()) if len(word) > 2}
        if name_words:
            scored.append((len(words & name_words) / len(name_words), course))
    best_score, best = max(scored, default=(0, None), key=lambda item: item[0])
    return best if best_score >= .6 else None


def is_ignored_course_item(state: dict[str, Any], title: str, calendar_name: str = "") -> bool:
    digits = re.sub(r"\D", "", f"{title} {calendar_name}")
    ignored = {key.split(":", 1)[1] for keys in state.get("ignored_course_keys", {}).values() for key in keys if key.startswith("catalog:")}
    return any(number in digits for number in ignored)


class FlexiHandler(BaseHTTPRequestHandler):
    store: Store
    static_dir: Path

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")

    def _json(self, payload: Any, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            return self._json(self.store.rollover())
        if path == "/api/candidates":
            return self._json(candidate_summaries(self.store.load()))
        filename = "index.html" if path == "/" else path.lstrip("/")
        target = (self.static_dir / filename).resolve()
        if self.static_dir.resolve() not in target.parents and target != self.static_dir.resolve():
            return self.send_error(HTTPStatus.FORBIDDEN)
        if not target.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        content = target.read_bytes()
        mime = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/schedule":
                strategy = body.get("strategy", "balanced")
                result = solve(self.store.load(), strategy)
                def apply(state):
                    preserved = [x for x in state.get("schedule", []) if x.get("locked") or x.get("completed")]
                    state["schedule"] = preserved + result["schedule"]
                    state["loss_report"] = result["loss_report"]
                    state["selected_candidate"] = strategy
                self.store.mutate(apply, record_history=False)
                return self._json(result)
            if path == "/api/undo":
                return self._json(self.store.undo())
            if path == "/api/sync":
                return self._json(self._sync())
            if path == "/api/task":
                task = body
                task["id"] = task.get("id") or self.store.new_id("task")
                task.setdefault("completed", False); task.setdefault("recurrence", "none"); task.setdefault("notes", "")
                self.store.mutate(lambda s: s["tasks"].append(task))
                return self._json(task, 201)
            if path == "/api/manual-block":
                block = body
                course_id = block.get("course_id")
                block["id"] = block.get("id") or self.store.new_id("manual")
                block.update({"kind": "study" if course_id else "task",
                              "owner_id": course_id or block["id"],
                              "completed": bool(block.get("completed", True)), "locked": True,
                              "severity": int(block.get("severity", 2)), "strategy": "manual"})
                block.pop("course_id", None)
                def add_manual(state):
                    state["schedule"].append(block)
                    if course_id and block["completed"]:
                        course = next(c for c in state["courses"] if c["id"] == course_id)
                        start, end = datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])
                        week = week_key(start)
                        course.setdefault("completed_hours", {})[week] = course.get("completed_hours", {}).get(week, 0) + (end-start).total_seconds()/3600
                self.store.mutate(add_manual)
                return self._json(block, 201)
            if path == "/api/move":
                return self._json(self._move(body["id"], body["start"], body["end"]))
            if path == "/api/course":
                course = body; course["id"] = course.get("id") or self.store.new_id("course"); course.setdefault("completed_hours", {})
                self.store.mutate(lambda s: s["courses"].append(course))
                return self._json(course, 201)
            if path == "/api/feed":
                feed = body; feed["id"] = feed.get("id") or self.store.new_id("feed")
                self.store.mutate(lambda s: s["calendar_feeds"].append(feed))
                return self._json(feed, 201)
            if path.startswith("/api/complete/"):
                item_id = path.rsplit("/", 1)[1]
                return self._json(self._complete(item_id, body.get("handoff", "")))
            if path == "/api/event-primary":
                event_id = body["id"]
                def primary(state):
                    selected = next((e for e in state["events"] if e["id"] == event_id), None)
                    if not selected: return
                    a, b = datetime.fromisoformat(selected["start"]), datetime.fromisoformat(selected["end"])
                    for event in state["events"]:
                        x, y = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
                        if a < y and x < b: event["primary"] = event["id"] == event_id
                self.store.mutate(primary)
                return self._json({"ok": True})
            if path == "/api/block-task":
                return self._json(self._assign_task(body["block_id"], body["task_id"], body.get("action", "add")))
            if path == "/api/block-course":
                return self._json(self._change_block_course(body["block_id"], body["course_id"]))
            if path == "/api/task-complete":
                task_id, completed = body["task_id"], bool(body.get("completed", True))
                def set_task_completed(state):
                    task = next((item for item in state.get("tasks", []) if item["id"] == task_id), None)
                    if not task: raise ValueError("Task not found")
                    task["completed"] = completed
                    return task
                return self._json(self.store.mutate(set_task_completed))
            if path == "/api/course-week":
                course_id, week, completed = body["course_id"], body["week"], bool(body.get("completed", True))
                def set_course_week(state):
                    if not any(course["id"] == course_id for course in state.get("courses", [])): raise ValueError("Course not found")
                    state.setdefault("course_week_status", {}).setdefault(week, {})[course_id] = completed
                    return {"course_id": course_id, "week": week, "completed": completed}
                return self._json(self.store.mutate(set_course_week))
            if path == "/api/item-status":
                return self._json(self._set_item_status(body["id"], body["status"]))
            return self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 400)

    def do_PUT(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/settings":
            self.store.mutate(lambda s: s["settings"].update(body))
        elif path == "/api/overrides":
            week, course_id, target = body["week"], body["course_id"], float(body["target"])
            self.store.mutate(lambda s: s["weekly_overrides"].setdefault(week, {}).update({course_id: target}))
        elif path.startswith("/api/course/"):
            course_id = path.rsplit("/", 1)[1]
            allowed = {key: value for key, value in body.items() if key in {"default_target", "difficulty", "color"}}
            if "default_target" in allowed: allowed["default_target"] = float(allowed["default_target"])
            self.store.mutate(lambda s: next(course for course in s["courses"] if course["id"] == course_id).update(allowed))
        elif path.startswith("/api/feed/"):
            feed_id = path.rsplit("/", 1)[1]
            self.store.mutate(lambda s: next(f for f in s["calendar_feeds"] if f["id"] == feed_id).update(body))
        else:
            return self.send_error(HTTPStatus.NOT_FOUND)
        return self._json(self.store.load())

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/course/"):
                course_id = path.rsplit("/", 1)[1]
                def remove(state):
                    course = next((c for c in state["courses"] if c["id"] == course_id), None)
                    if not course: raise ValueError("Course not found")
                    feed_id, course_key = course.get("source_feed_id"), course.get("course_key")
                    if feed_id and course_key:
                        ignored = state.setdefault("ignored_course_keys", {}).setdefault(feed_id, [])
                        if course_key not in ignored:
                            ignored.append(course_key)
                        state["events"] = [e for e in state.get("events", []) if not (e.get("feed_id") == feed_id and e.get("course_key") == course_key)]
                    state["courses"] = [c for c in state["courses"] if c["id"] != course_id]
                    state["schedule"] = [x for x in state["schedule"] if x.get("owner_id") != course_id]
                    for week in state.get("weekly_overrides", {}).values(): week.pop(course_id, None)
                    for task in state.get("tasks", []):
                        if task.get("course_id") == course_id: task["course_id"] = None
                    state.get("handoffs", {}).pop(course_id, None)
                self.store.mutate(remove)
                return self._json({"ok": True})
            if path.startswith("/api/task/"):
                task_id = path.rsplit("/", 1)[1]
                def remove_task(state):
                    task = next((task for task in state.get("tasks", []) if task["id"] == task_id), None)
                    if not task: raise ValueError("Task not found")
                    if task.get("feed_id"):
                        ignored = state.setdefault("ignored_task_ids", {}).setdefault(task["feed_id"], [])
                        if task_id not in ignored: ignored.append(task_id)
                    state["tasks"] = [task for task in state["tasks"] if task["id"] != task_id]
                    state["schedule"] = [item for item in state.get("schedule", []) if item.get("owner_id") != task_id]
                    for item in state.get("schedule", []):
                        if task_id in item.get("task_ids", []): item["task_ids"].remove(task_id)
                self.store.mutate(remove_task)
                return self._json({"ok": True})
            if path.startswith("/api/block/"):
                item_id = path.rsplit("/", 1)[1]
                self.store.mutate(lambda s: s.update(schedule=[x for x in s["schedule"] if x["id"] != item_id]))
                return self._json({"ok": True})
            if path.startswith("/api/feed/"):
                feed_id = path.rsplit("/", 1)[1]
                def remove_feed(state):
                    if not any(f["id"] == feed_id for f in state["calendar_feeds"]): raise ValueError("Calendar feed not found")
                    auto_course_ids = {c["id"] for c in state["courses"] if c.get("source_feed_id") == feed_id}
                    state["calendar_feeds"] = [f for f in state["calendar_feeds"] if f["id"] != feed_id]
                    state["events"] = [e for e in state["events"] if e.get("feed_id") != feed_id]
                    state["tasks"] = [t for t in state["tasks"] if t.get("feed_id") != feed_id]
                    state["courses"] = [c for c in state["courses"] if c["id"] not in auto_course_ids]
                    state["schedule"] = [x for x in state["schedule"] if x.get("owner_id") not in auto_course_ids]
                    state["event_overrides"] = {k:v for k,v in state.get("event_overrides", {}).items() if not k.startswith(f"ical-{feed_id}-")}
                    state.get("ignored_course_keys", {}).pop(feed_id, None)
                    state.get("ignored_task_ids", {}).pop(feed_id, None)
                self.store.mutate(remove_feed)
                return self._json({"ok": True})
            return self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 404)

    def _sync(self):
        state = self.store.load(); errors = []; imported = []; canvas_tasks = []
        refreshed_ids = []
        for feed in state["calendar_feeds"]:
            if not feed.get("enabled", True) or feed.get("enforcement") == "hidden": continue
            try:
                feed_events = fetch_feed(feed)
                refreshed_ids.append(feed["id"])
                names = sorted({e.get("calendar_name", feed.get("name", "Calendar")) for e in feed_events})
                feed["discovered_calendars"] = names
                rules = feed.setdefault("calendar_rules", {})
                visible_events = []
                for event in feed_events:
                    rule = rules.get(event["calendar_name"], feed.get("enforcement", "hard"))
                    if rule == "hidden": continue
                    event["enforcement"] = rule
                    visible_events.append(event)
                feed_events = visible_events
                if feed.get("kind") == "class":
                    palette = ["#3B82F6", "#10B981", "#8B5CF6", "#F97316", "#EC4899", "#06B6D4"]
                    desired: dict[str, str] = {}
                    ignored_keys = set(state.get("ignored_course_keys", {}).get(feed["id"], []))
                    for event in feed_events:
                        if event.get("all_day"):
                            event["kind"] = "calendar"
                            continue
                        key, name, meeting_type = class_details(event["title"])
                        if key in ignored_keys:
                            event["_ignored_course"] = True
                            continue
                        event.update({"course_key": key, "course_name": name, "meeting_type": meeting_type})
                        if key not in desired or (meeting_type != "lab" and "lab" in desired[key].casefold()):
                            desired[key] = name
                    feed_courses = [c for c in state["courses"] if c.get("source_feed_id") == feed["id"]]
                    by_key: dict[str, list[dict[str, Any]]] = {}
                    for course in feed_courses:
                        old_key = course.get("course_key") or class_details(course["name"])[0]
                        by_key.setdefault(old_key, []).append(course)
                    course_for_key: dict[str, dict[str, Any]] = {}
                    duplicate_ids: dict[str, str] = {}
                    for key, name in sorted(desired.items()):
                        matches = by_key.get(key, [])
                        course = matches[0] if matches else {"id": self.store.new_id("course"), "color": palette[len(state["courses"]) % len(palette)],
                            "difficulty": 3, "default_target": 6.0, "completed_hours": {}, "source_feed_id": feed["id"]}
                        if not matches:
                            state["courses"].append(course)
                        course.update({"name": name, "course_key": key})
                        course_for_key[key] = course
                        for duplicate in matches[1:]:
                            duplicate_ids[duplicate["id"]] = course["id"]
                            for week, hours in duplicate.get("completed_hours", {}).items():
                                course.setdefault("completed_hours", {})[week] = course.get("completed_hours", {}).get(week, 0) + hours
                    if duplicate_ids:
                        state["courses"] = [c for c in state["courses"] if c["id"] not in duplicate_ids]
                        for block in state.get("schedule", []):
                            if block.get("owner_id") in duplicate_ids:
                                block["owner_id"] = duplicate_ids[block["owner_id"]]
                    for event in feed_events:
                        if event.get("course_key"):
                            event["owner_id"] = course_for_key[event["course_key"]]["id"]
                if feed.get("kind") == "canvas":
                    for event in feed_events:
                        if is_ignored_course_item(state, event["title"], event.get("calendar_name", "")):
                            event["_ignored_course"] = True
                            continue
                        course = course_for_calendar_item(state["courses"], event["title"], event.get("calendar_name", ""))
                        task_id = f"canvas-task-{event['id']}"
                        if task_id in state.get("ignored_task_ids", {}).get(feed["id"], []):
                            continue
                        canvas_tasks.append({
                            "id": task_id, "title": f"Finish {event['title']}",
                            "course_id": course["id"] if course else None, "constraint_type": "flexible",
                            "start": None, "end": None, "deadline": event["start"], "duration_minutes": 90,
                            "severity": 5, "difficulty": course.get("difficulty", 4) if course else 4,
                            "recurrence": "none", "completed": False,
                            "notes": f"Focus: complete before the 24-hour protected deadline.", "feed_id": feed["id"],
                        })
                for event in feed_events:
                    if not event.pop("_ignored_course", False):
                        imported.append(event)
            except Exception as exc: errors.append(f"{feed.get('name', 'Feed')}: {exc}")
        for task in canvas_tasks:
            if not task.get("course_id"):
                course = course_for_calendar_item(state["courses"], task["title"])
                if course:
                    task["course_id"] = course["id"]
        overrides = state.setdefault("event_overrides", {})
        for event in imported:
            if event["id"] in overrides: event.update(overrides[event["id"]])
        state["events"] = [e for e in state["events"] if e.get("feed_id") not in refreshed_ids] + imported
        prior_tasks = {t["id"]: t for t in state["tasks"]}
        state["tasks"] = [t for t in state["tasks"] if t.get("feed_id") not in refreshed_ids]
        for task in canvas_tasks:
            prior = prior_tasks.get(task["id"], {})
            for key in ("completed", "assigned_block_id", "auto_place"):
                if key in prior:
                    task[key] = prior[key]
            state["tasks"].append(task)
        state["meta"]["last_sync"] = iso_now()
        # A refresh is atomic from the user's perspective: new calendar locks and
        # the dependent flexible plan are persisted together.
        result = solve(state, state.get("selected_candidate", "balanced"))
        preserved = [x for x in state.get("schedule", []) if x.get("locked") or x.get("completed")]
        state["schedule"] = preserved + result["schedule"]
        state["loss_report"] = result["loss_report"]
        self.store.save(state)
        return {"imported": len(imported), "errors": errors, "last_sync": state["meta"]["last_sync"],
                "scheduled": len(result["schedule"]), "status": result["status"]}

    def _assign_task(self, block_id: str, task_id: str, action: str):
        def apply(state):
            block = next((item for item in state.get("schedule", []) if item["id"] == block_id and item.get("kind") == "study"), None)
            task = next((item for item in state.get("tasks", []) if item["id"] == task_id and (action == "remove" or not item.get("completed"))), None)
            if not block or not task:
                raise ValueError("Study block or task not found")
            task_ids = block.setdefault("task_ids", [])
            if action == "remove":
                if task_id in task_ids:
                    task_ids.remove(task_id)
                task.pop("assigned_block_id", None)
                task["auto_place"] = False
                return block
            previous = task.get("assigned_block_id")
            if previous and previous != block_id:
                raise ValueError("That task is already assigned to another block")
            if task_id not in task_ids:
                task_ids.append(task_id)
            task["assigned_block_id"] = block_id
            task["auto_place"] = False
            if not task.get("course_id"):
                task["course_id"] = block["owner_id"]
            block["locked"] = True
            state["schedule"] = [item for item in state["schedule"] if item["id"] == block_id or item.get("owner_id") != task_id]
            return block
        return self.store.mutate(apply)

    def _change_block_course(self, block_id: str, course_id: str):
        def apply(state):
            block = next((item for item in state.get("schedule", []) if item["id"] == block_id and item.get("kind") == "study"), None)
            course = next((item for item in state.get("courses", []) if item["id"] == course_id), None)
            if not block or not course: raise ValueError("Study block or course not found")
            replaced_course_id = block["owner_id"]
            for task_id in block.get("task_ids", []):
                task = next((item for item in state.get("tasks", []) if item["id"] == task_id), None)
                if task:
                    task.pop("assigned_block_id", None)
                    task["auto_place"] = True
            block.update({"owner_id": course_id, "replaced_course_id": replaced_course_id, "title": f"Study {course_label(course)}", "task_ids": [], "locked": True, "strategy": "manual"})
            return block
        return self.store.mutate(apply)

    def _set_item_status(self, item_id: str, status: str):
        if status not in {"missed", "skipped"}:
            raise ValueError("Unsupported item status")
        def apply(state):
            item = next((entry for entry in state.get("schedule", []) if entry["id"] == item_id), None)
            if not item:
                task = next((entry for entry in state.get("tasks", []) if entry["id"] == item_id), None)
                if not task: raise ValueError("Schedule item not found")
                if status == "missed":
                    task.update({"constraint_type": "flexible", "start": None, "end": None})
                else:
                    task.update({"completed": True, "skipped": True})
                return task
            if item.get("kind") == "routine":
                item.update({status: True, "locked": True})
                return item
            if item.get("kind") == "task":
                task = next((task for task in state.get("tasks", []) if task["id"] == item.get("owner_id")), None)
                if task and status == "skipped":
                    task.update({"completed": True, "skipped": True})
            if item.get("kind") == "study" and status == "skipped":
                course = next(course for course in state["courses"] if course["id"] == item["owner_id"])
                start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
                week = week_key(start)
                current = float(state.get("weekly_overrides", {}).get(week, {}).get(course["id"], course["default_target"]))
                completed = float(course.get("completed_hours", {}).get(week, 0))
                duration = (end - start).total_seconds() / 3600
                state.setdefault("weekly_overrides", {}).setdefault(week, {})[course["id"]] = max(completed, current - duration)
            state["schedule"] = [entry for entry in state["schedule"] if entry["id"] != item_id]
            return {"id": item_id, "status": status}
        return self.store.mutate(apply)

    def _complete(self, item_id: str, handoff: str):
        def apply(state):
            item = next((x for x in state["schedule"] if x["id"] == item_id), None)
            if not item: raise ValueError("Schedule item not found")
            item["completed"] = True; item["locked"] = True
            if item["kind"] == "study":
                course = next(c for c in state["courses"] if c["id"] == item["owner_id"])
                week = week_key(datetime.fromisoformat(item["start"]))
                hours = (datetime.fromisoformat(item["end"]) - datetime.fromisoformat(item["start"])).total_seconds() / 3600
                course.setdefault("completed_hours", {})[week] = course.get("completed_hours", {}).get(week, 0) + hours
                if handoff: state["handoffs"][item["owner_id"]] = handoff
                attached = set(item.get("task_ids", []))
                for task in state.get("tasks", []):
                    if task["id"] in attached:
                        task["completed"] = True
            else:
                task = next((t for t in state["tasks"] if t["id"] == item["owner_id"]), None)
                if task: task["completed"] = True
            return item
        return self.store.mutate(apply)

    def _move(self, item_id: str, start: str, end: str):
        def apply(state):
            if item_id.startswith("routine-"):
                state.setdefault("routine_overrides", {})[item_id] = {"start": start, "end": end}
                return {"id": item_id, "start": start, "end": end, "locked": True}
            item = next((x for x in state.get("schedule", []) if x["id"] == item_id), None)
            if item:
                item.update({"start": start, "end": end, "locked": True, "strategy": "manual"})
                return item
            task = next((x for x in state.get("tasks", []) if x["id"] == item_id), None)
            if task:
                task.update({"start": start, "end": end, "constraint_type": "fixed"})
                return task
            event = next((x for x in state.get("events", []) if x["id"] == item_id), None)
            if event:
                event.update({"start": start, "end": end})
                state.setdefault("event_overrides", {})[item_id] = {"start": start, "end": end}
                return event
            raise ValueError("Calendar item not found")
        return self.store.mutate(apply)

