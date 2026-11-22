import subprocess
import unittest

from tools.eggtv_publish import push_with_retry


class PushTests(unittest.TestCase):
    def fake_git(self, failures, remote="parent"):
        calls, outcomes = [], iter(failures)

        def run(args):
            calls.append(args)
            if args[0] == "rev-parse":
                return subprocess.CompletedProcess(args, 0, "head" if args[1] == "HEAD" else "parent", "")
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess(args, 0, remote + "\trefs/heads/main\n", "")
            self.assertEqual(args, ["push", "origin", "HEAD:refs/heads/main"])
            error = next(outcomes)
            return subprocess.CompletedProcess(args, 1 if error else 0, "", error)
        return run, calls

    def test_internal_server_error_is_retried_without_force(self):
        run, calls = self.fake_git(["remote: Internal Server Error", "HTTP 503", ""])
        delays = []
        push_with_retry(".", run=run, sleep=delays.append)
        self.assertEqual(len([call for call in calls if call[0] == "push"]), 3)
        self.assertEqual(delays, [2, 4])
        self.assertFalse(any("--force" in call for call in calls))

    def test_lost_response_after_success_is_recognized(self):
        run, calls = self.fake_git(["RPC failed"], remote="head")
        push_with_retry(".", run=run, sleep=lambda _: self.fail("Already pushed"))
        self.assertEqual(len([call for call in calls if call[0] == "push"]), 1)

    def test_concurrent_remote_edit_is_preserved(self):
        run, _ = self.fake_git(["fetch first"], remote="somebody-elses-commit")
        with self.assertRaisesRegex(RuntimeError, "没有覆盖远端"):
            push_with_retry(".", run=run, sleep=lambda _: self.fail("Do not retry concurrent edits"))

    def test_permission_failures_are_not_retried(self):
        run, calls = self.fake_git(["HTTP 403: permission denied"])
        with self.assertRaises(RuntimeError):
            push_with_retry(".", run=run, sleep=lambda _: self.fail("Do not retry authentication"))
        self.assertEqual(len([call for call in calls if call[0] == "push"]), 1)

    def test_timeout_is_retried_and_retry_budget_is_bounded(self):
        run, _ = self.fake_git(["", ""])
        count = [0]
        def timed_out(args):
            if args[0] == "push" and count[0] == 0:
                count[0] += 1
                raise subprocess.TimeoutExpired("git push", 120)
            return run(args)
        delays = []
        push_with_retry(".", run=timed_out, sleep=delays.append)
        self.assertEqual(delays, [2])
        run, calls = self.fake_git(["HTTP 503"] * 4)
        with self.assertRaises(RuntimeError):
            push_with_retry(".", run=run, sleep=lambda _: None)
        self.assertEqual(len([call for call in calls if call[0] == "push"]), 4)
