"""Scheduler: two at a time, the rest wait in click order."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from core.scheduler import Scheduler  # noqa: E402


class Harness:
    def __init__(self, limit=2):
        self.scheduler, self.started, self.finish = Scheduler(limit), [], {}

    def add(self, key):
        def run(finished):
            self.started.append(key)
            self.finish[key] = finished
        return self.scheduler.add(key, run)


class SchedulerTest(unittest.TestCase):
    def test_two_run_at_once_and_the_rest_wait(self):
        h = Harness()
        for key in "abcde":
            h.add(key)
        self.assertEqual(h.started, ["a", "b"])
        self.assertTrue(h.scheduler.queued("c"))
        self.assertFalse(h.scheduler.queued("a"))

    def test_waiting_jobs_start_in_click_order_as_slots_free(self):
        h = Harness()
        for key in "abcde":
            h.add(key)
        h.finish["b"]()
        self.assertEqual(h.started, ["a", "b", "c"])
        h.finish["a"]()
        h.finish["c"]()
        self.assertEqual(h.started, ["a", "b", "c", "d", "e"])

    def test_a_cancelled_waiting_job_never_starts(self):
        h = Harness()
        for key in "abcd":
            h.add(key)
        self.assertTrue(h.scheduler.cancel("c"))
        h.finish["a"]()
        h.finish["b"]()
        self.assertEqual(h.started, ["a", "b", "d"])

    def test_cancelling_a_job_that_is_not_waiting_does_nothing(self):
        h = Harness()
        h.add("a")
        self.assertFalse(h.scheduler.cancel("a"))   # already running
        self.assertFalse(h.scheduler.cancel("zzz"))  # unknown

    def test_adding_the_same_job_twice_is_ignored(self):
        h = Harness()
        for key in "aaabbb":
            h.add(key)
        self.assertEqual(h.started, ["a", "b"])
        self.assertFalse(h.add("b"))
        h.add("c")
        self.assertFalse(h.add("c"))

    def test_finishing_twice_does_not_free_two_slots(self):
        h = Harness()
        for key in "abcd":
            h.add(key)
        h.finish["a"]()
        h.finish["a"]()
        self.assertEqual(h.started, ["a", "b", "c"])

    def test_a_job_that_finishes_immediately_lets_the_next_start(self):
        s, started = Scheduler(1), []
        s.add("a", lambda finished: (started.append("a"), finished()))
        s.add("b", lambda finished: started.append("b"))
        self.assertEqual(started, ["a", "b"])

    def test_a_job_that_fails_to_start_frees_its_slot(self):
        s, started = Scheduler(1), []

        def broken(finished):
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            s.add("a", broken)
        s.add("b", lambda finished: started.append("b"))
        self.assertEqual(started, ["b"])


if __name__ == "__main__":
    unittest.main()
