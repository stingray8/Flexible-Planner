import json
import ssl
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flexistudy.api import FlexiHandler, class_details, course_for_calendar_item, is_ignored_course_item
from flexistudy.ical import fetch_feed, parse_ics
from flexistudy.scheduler import solve
from flexistudy.store import Store, initial_state


class FlexiStudyTests(unittest.TestCase):
    def setUp(self):
        self.state = initial_state(date(2026, 11, 8))

    def test_store_round_trip_is_versioned(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            state = store.load()
            state["settings"]["rest_hours"] = 2.75
            store.save(state)
            loaded = json.loads(store.path.read_text())
            self.assertEqual(loaded["schema_version"], "1.0")
            self.assertEqual(loaded["settings"]["rest_hours"], 2.75)

    def test_store_undo_restores_previous_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            original = store.load()["settings"]["sleep_hours"]
            store.mutate(lambda state: state["settings"].update(sleep_hours=6.0))
            self.assertEqual(store.load()["settings"]["sleep_hours"], 6.0)
            store.undo()
            self.assertEqual(store.load()["settings"]["sleep_hours"], original)

    def test_study_block_can_switch_subject_and_be_undone(self):
        result = solve(self.state, "balanced")
        block = next(item for item in result["schedule"] if item["kind"] == "study")
        original_course = block["owner_id"]
        replacement = next(course for course in self.state["courses"] if course["id"] != original_course)
        self.state["schedule"] = [block]
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json"); store.save(self.state)
            handler = object.__new__(FlexiHandler); handler.store = store
            handler._change_block_course(block["id"], replacement["id"])
            self.assertEqual(store.load()["schedule"][0]["owner_id"], replacement["id"])
            replanned = solve(store.load(), "balanced")
            replacement_blocks = [item for item in replanned["schedule"] if item["kind"] == "study" and item["owner_id"] == original_course]
            self.assertTrue(replacement_blocks)
            self.assertTrue(all(datetime.fromisoformat(item["start"]) >= datetime.fromisoformat(block["end"]) for item in replacement_blocks))
            store.undo()
            self.assertEqual(store.load()["schedule"][0]["owner_id"], original_course)

    def test_planning_week_runs_sunday_through_saturday(self):
        state = initial_state(date(2026, 11, 11))
        self.assertEqual(state["meta"]["active_week"], "2026-11-08")
        self.assertEqual((date.fromisoformat(state["meta"]["active_week"]) + timedelta(days=6)).strftime("%A"), "Saturday")

    def test_solver_is_deterministic_and_serves_every_course(self):
        first = solve(self.state, "balanced")
        second = solve(self.state, "balanced")
        self.assertEqual(first["schedule"], second["schedule"])
        served = {item["owner_id"] for item in first["schedule"] if item["kind"] == "study"}
        self.assertEqual(served, {course["id"] for course in self.state["courses"]})

    def test_solver_concentrates_each_subject_into_fewer_days(self):
        result = solve(self.state, "balanced")
        for course in self.state["courses"]:
            study_days = {datetime.fromisoformat(item["start"]).date() for item in result["schedule"] if item["kind"] == "study" and item["owner_id"] == course["id"]}
            self.assertLessEqual(len(study_days), 4)

    def test_ical_event_is_ingested(self):
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:lecture-1
DTSTART:20261109T140000Z
DTEND:20261109T150000Z
SUMMARY:Algorithms Lecture
CATEGORIES:School
END:VEVENT
END:VCALENDAR"""
        events = parse_ics(text, {"id": "school", "name": "School", "enforcement": "hard"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Algorithms Lecture")
        self.assertEqual(events[0]["enforcement"], "hard")
        self.assertEqual(events[0]["calendar_name"], "School")

    def test_weekly_recurrence_expands_and_honors_exdates(self):
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:weekly-1
DTSTART;TZID=America/New_York:20261027T120000
DTEND;TZID=America/New_York:20261027T125000
RRULE:FREQ=WEEKLY;UNTIL=20261111T235959Z;BYDAY=TU,TH
EXDATE;TZID=America/New_York:20261105T120000
SUMMARY:Rec. Physics II :: 33142 E
END:VEVENT
END:VCALENDAR"""
        events = parse_ics(text, {"id": "school", "name": "School", "kind": "class"})
        starts = [event["start"] for event in events]
        self.assertEqual(len(events), 4)
        self.assertFalse(any(start.startswith("2026-11-05") for start in starts))
        self.assertTrue(any(start == "2026-11-10T12:00:00-05:00" for start in starts))

    def test_lecture_and_recitation_share_course_identity(self):
        lecture = class_details("Physics II for Engineering and Physics Students :: 33142 1")
        recitation = class_details("Rec. Physics II for Engineering and Physics Students :: 33142 E")
        self.assertEqual(lecture[:2], recitation[:2])
        self.assertEqual(lecture[2], "lecture")
        self.assertEqual(recitation[2], "recitation")

    def test_mse_lab_shares_the_27100_course(self):
        lecture = class_details("Engineering the Materials of the Future :: 27100 1")
        labeled_lab = class_details("MSE Lab :: 27100")
        short_lab = class_details("MSE Lab")
        self.assertEqual(lecture[0], labeled_lab[0])
        self.assertEqual(lecture[0], short_lab[0])
        self.assertEqual(short_lab[2], "lab")

    def test_canvas_code_matches_course_and_assignment_is_put_in_study_block(self):
        course = self.state["courses"][0]
        course["course_key"] = "catalog:15112"
        self.assertIs(course_for_calendar_item(self.state["courses"], "[15-112] Homework 4"), course)
        assignment = {
            "id": "canvas-hw4", "title": "Finish [15-112] Homework 4", "course_id": course["id"],
            "constraint_type": "flexible", "duration_minutes": 60, "severity": 5, "difficulty": 3,
            "completed": False, "feed_id": "canvas", "deadline": "2026-11-13T20:00:00",
        }
        self.state["tasks"].append(assignment)
        result = solve(self.state, "balanced")
        containing = [block for block in result["schedule"] if assignment["id"] in block.get("task_ids", [])]
        self.assertEqual(len(containing), 1)
        self.assertEqual(containing[0]["owner_id"], course["id"])

    def test_manual_task_assignment_completes_with_study_block(self):
        result = solve(self.state, "balanced")
        block = next(item for item in result["schedule"] if item["kind"] == "study")
        task = self.state["tasks"][0]
        block["task_ids"] = [task["id"]]
        self.state["schedule"] = [block]
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            store.save(self.state)
            handler = object.__new__(FlexiHandler)
            handler.store = store
            handler._complete(block["id"], "Continue with the next section")
            saved = store.load()
            self.assertTrue(next(item for item in saved["tasks"] if item["id"] == task["id"])["completed"])
            self.assertEqual(saved["handoffs"][block["owner_id"]], "Continue with the next section")

    def test_protected_routines_move_around_fixed_events(self):
        week = date.fromisoformat(self.state["meta"]["active_week"])
        self.state["events"].append({
            "id": "fixed-lunch-conflict", "title": "Class", "enforcement": "hard",
            "start": f"{week.isoformat()}T12:00:00", "end": f"{week.isoformat()}T13:00:00",
        })
        result = solve(self.state, "balanced")
        routines = [item for item in result["schedule"] if item["kind"] == "routine"]
        self.assertEqual(len(routines), 42)
        lunch = next(item for item in routines if item["id"] == f"routine-lunch-{week.isoformat()}")
        self.assertEqual((datetime.fromisoformat(lunch["end"]) - datetime.fromisoformat(lunch["start"])).total_seconds() / 60, 45)
        self.assertGreaterEqual(datetime.fromisoformat(lunch["start"]).time(), datetime.strptime("13:00", "%H:%M").time())

    def test_failed_refresh_preserves_last_good_feed_events(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            state = store.load()
            state["calendar_feeds"] = [{"id": "school", "name": "School", "url": "https://invalid", "enabled": True, "enforcement": "hard"}]
            state["events"] = [{"id": "old", "feed_id": "school", "title": "Last good copy", "start": "2026-11-09T10:00:00", "end": "2026-11-09T11:00:00", "enforcement": "hard"}]
            store.save(state)
            handler = object.__new__(FlexiHandler)
            handler.store = store
            with patch("flexistudy.api.fetch_feed", side_effect=OSError("offline")):
                result = handler._sync()
            self.assertEqual(result["imported"], 0)
            self.assertEqual(store.load()["events"][0]["title"], "Last good copy")

    def test_deleted_imported_course_stays_ignored_on_refresh(self):
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:physics
DTSTART:20261109T100000
DTEND:20261109T105000
SUMMARY:Physics II :: 33142 1
END:VEVENT
END:VCALENDAR"""
        feed = {"id": "classes", "name": "Classes", "url": "fixture", "kind": "class", "enforcement": "hard", "enabled": True}
        events = parse_ics(text, feed)
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            state = store.load()
            state["calendar_feeds"] = [feed]
            state["ignored_course_keys"] = {"classes": ["catalog:33142"]}
            store.save(state)
            handler = object.__new__(FlexiHandler)
            handler.store = store
            with patch("flexistudy.api.fetch_feed", return_value=events):
                handler._sync()
            saved = store.load()
            self.assertFalse(any(course.get("course_key") == "catalog:33142" for course in saved["courses"]))
            self.assertFalse(any(event.get("feed_id") == "classes" for event in saved["events"]))
            self.assertTrue(is_ignored_course_item(saved, "[33-142] Homework 7"))

    def test_deleted_canvas_task_stays_out_of_backlog_after_refresh(self):
        text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:homework-7
DTSTART:20261113T200000
DTEND:20261113T210000
SUMMARY:[15-112] Homework 7
END:VEVENT
END:VCALENDAR"""
        feed = {"id": "canvas", "name": "Canvas", "url": "fixture", "kind": "canvas", "enforcement": "flexible", "enabled": True}
        events = parse_ics(text, feed)
        ignored_id = f"canvas-task-{events[0]['id']}"
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json")
            state = store.load(); state["calendar_feeds"] = [feed]; state["ignored_task_ids"] = {"canvas": [ignored_id]}; store.save(state)
            handler = object.__new__(FlexiHandler); handler.store = store
            with patch("flexistudy.api.fetch_feed", return_value=events): handler._sync()
            self.assertFalse(any(task["id"] == ignored_id for task in store.load()["tasks"]))

    def test_missed_fixed_task_becomes_flexible_for_replanning(self):
        task = self.state["tasks"][0]
        task.update({"constraint_type": "fixed", "start": "2026-11-08T09:00:00", "end": "2026-11-08T09:45:00"})
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "state.json"); store.save(self.state)
            handler = object.__new__(FlexiHandler); handler.store = store
            handler._set_item_status(task["id"], "missed")
            saved = next(item for item in store.load()["tasks"] if item["id"] == task["id"])
            self.assertEqual(saved["constraint_type"], "flexible")
            self.assertIsNone(saved["start"])

    def test_macos_native_tls_fallback_keeps_private_url_out_of_arguments(self):
        ics = b"""BEGIN:VCALENDAR
BEGIN:VEVENT
UID:fallback-1
DTSTART:20261109T140000Z
DTEND:20261109T150000Z
SUMMARY:Secure Feed Event
END:VEVENT
END:VCALENDAR"""
        failure = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        completed = subprocess.CompletedProcess(["/usr/bin/curl"], 0, ics, b"")
        private_url = "webcal://calendar.example/private-token.ics"
        with patch("flexistudy.ical.urllib.request.urlopen", side_effect=failure), \
             patch("flexistudy.ical.subprocess.run", return_value=completed) as run, \
             patch("flexistudy.ical.sys.platform", "darwin"), \
             patch("flexistudy.ical.Path.exists", return_value=True):
            events = fetch_feed({"id": "secure", "name": "Secure", "url": private_url, "enforcement": "hard"})
        self.assertEqual(events[0]["title"], "Secure Feed Event")
        self.assertNotIn("private-token", " ".join(run.call_args.args[0]))
        self.assertIn(b"private-token", run.call_args.kwargs["input"])

    def test_week_override_changes_required_hours(self):
        week = self.state["meta"]["active_week"]
        self.state["weekly_overrides"] = {week: {"cs101": 2.0}}
        result = solve(self.state, "balanced")
        cs_hours = sum((__import__('datetime').datetime.fromisoformat(x["end"]) - __import__('datetime').datetime.fromisoformat(x["start"])).total_seconds()/3600 for x in result["schedule"] if x["owner_id"] == "cs101")
        self.assertLessEqual(cs_hours, 12.0)  # 2h this week + 10h next week


if __name__ == "__main__":
    unittest.main()
