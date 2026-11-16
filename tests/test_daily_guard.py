import contextlib
from datetime import datetime, timezone
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse


_SPEC = importlib.util.spec_from_file_location(
    "ncmp_daily_guard", Path(__file__).resolve().parents[1] / "backup-scheduler" / "daily_guard.py"
)
guard = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = guard
_SPEC.loader.exec_module(guard)

TODAY = "2026-11-16"
NOW = datetime(2026, 11, 15, 18, 45, tzinfo=timezone.utc)


def artifact(run_id=123, **updates):
    value = {
        "name": guard.MARKER_PREFIX + TODAY,
        "expired": False,
        "created_at": "2026-11-15T18:40:00Z",
        "workflow_run": {"id": run_id, "head_branch": "main"},
    }
    value.update(updates)
    return value


def workflow_run(**updates):
    value = {
        "id": 123,
        "path": guard.WORKFLOW_PATH,
        "head_branch": "main",
        "conclusion": "success",
        "run_started_at": "2026-11-15T17:01:00Z",
    }
    value.update(updates)
    return value


class FakeClient:
    repository_path = "/repos/hhhhhades/ncmp"

    def __init__(self, pages=None, runs=None):
        self.pages = pages or {1: []}
        self.runs = runs or {}
        self.paths = []

    def get_json(self, path):
        self.paths.append(path)
        if "/actions/artifacts?" in path:
            query = parse_qs(urlparse(path).query)
            assert query["name"] == [guard.MARKER_PREFIX + TODAY]
            assert query["per_page"] == ["100"]
            return {"artifacts": self.pages[int(query["page"][0])]}
        return self.runs[int(path.rsplit("/", 1)[1])]


class DailyGuardTests(unittest.TestCase):
    def decision(self, client, **kwargs):
        return guard.evaluate_guard(client, now=NOW, **kwargs)

    def test_beijing_day_crosses_utc_midnight_without_tzdata(self):
        self.assertEqual(guard.beijing_date("2026-11-15T16:00:00Z"), TODAY)
        self.assertEqual(guard.beijing_date("2026-11-15T15:59:59Z"), "2026-11-15")

    def test_manual_without_date_is_guarded(self):
        client = FakeClient({1: [artifact()]}, {123: workflow_run()})
        result = self.decision(client)
        self.assertFalse(result.should_run)
        self.assertEqual(result.score_date, TODAY)
        self.assertIn("completed", result.reason)

    def test_successful_skipped_workflow_is_not_completion(self):
        client = FakeClient()
        self.assertTrue(self.decision(client).should_run)
        self.assertTrue(all("/actions/workflows/" not in path for path in client.paths))
        # Repeated checks and verification skips do not create completion markers.
        self.assertTrue(self.decision(client).should_run)
        self.assertEqual(client.pages, {1: []})

    def test_run_start_and_conclusion_do_not_override_marker(self):
        client = FakeClient({1: [artifact()]}, {123: workflow_run(
            run_started_at="2026-11-14T23:00:00Z", conclusion="failure"
        )})
        self.assertFalse(self.decision(client).should_run)

    def test_marker_creation_day_is_checked(self):
        client = FakeClient({1: [artifact(created_at="2026-11-15T15:59:59Z")]})
        self.assertTrue(self.decision(client).should_run)
        self.assertEqual(len(client.paths), 1)

    def test_wrong_marker_name_and_expired_artifacts_do_not_count(self):
        client = FakeClient({1: [artifact(name=guard.MARKER_PREFIX + TODAY + "-other"), artifact(expired=True)]})
        self.assertTrue(self.decision(client).should_run)

    def test_artifact_from_another_branch_does_not_count(self):
        client = FakeClient({1: [artifact(workflow_run={"id": 123, "head_branch": "feature"})]})
        self.assertTrue(self.decision(client).should_run)
        self.assertEqual(len(client.paths), 1)

    def test_run_branch_and_workflow_path_are_verified(self):
        for run in (workflow_run(head_branch="feature"), workflow_run(path=".github/workflows/other.yml"),
                    workflow_run(id=456), workflow_run(id="123")):
            with self.subTest(run=run):
                self.assertTrue(self.decision(FakeClient({1: [artifact()]}, {123: run})).should_run)

    def test_completion_on_later_page_is_found(self):
        first_page = [artifact(expired=True) for _ in range(100)]
        client = FakeClient({1: first_page, 2: [artifact()]}, {123: workflow_run()})
        self.assertFalse(self.decision(client).should_run)
        self.assertEqual(len(client.paths), 3)
        self.assertIn("page=2", client.paths[1])

    def test_pagination_limit_fails_closed(self):
        full_page = [artifact(expired=True) for _ in range(100)]
        client = FakeClient({1: full_page, 2: full_page})
        with patch.object(guard, "MAX_ARTIFACT_PAGES", 2):
            with self.assertRaisesRegex(guard.GuardError, "pagination limit"):
                self.decision(client)
        self.assertEqual(len(client.paths), 2)

    def test_backup_requires_explicit_valid_day(self):
        for day in ("", "2026-11-31", "2026-1-16", "invalid"):
            with self.subTest(day=day), self.assertRaises(guard.GuardError):
                self.decision(FakeClient(), trigger_source="backup", requested_score_date=day)

    def test_stale_and_future_backup_and_manual_requests_skip(self):
        for source in ("backup", "manual"):
            for day in ("2026-11-15", "2026-11-17"):
                client = FakeClient()
                result = self.decision(client, trigger_source=source, requested_score_date=day)
                self.assertFalse(result.should_run)
                self.assertIn("not today", result.reason)
                self.assertEqual(client.paths, [])

    def test_delayed_native_schedule_uses_today_and_deduplicates(self):
        client = FakeClient({1: [artifact()]}, {123: workflow_run()})
        self.assertFalse(self.decision(client, event_name="schedule", trigger_source="").should_run)

    def test_verification_queries_guard_but_never_scores_or_creates_marker(self):
        for artifacts, runs, expected in (([], {}, "true"), ([artifact()], {123: workflow_run()}, "false")):
            client = FakeClient({1: artifacts}, runs)
            result = self.decision(client, verification_only=True)
            self.assertFalse(result.should_run)
            self.assertIn("verification only; would_run=" + expected, result.reason)
            self.assertTrue(client.paths)
            self.assertTrue(all("/actions/artifacts?" in path or "/actions/runs/" in path for path in client.paths))

    def test_malformed_artifacts_fail_closed(self):
        for value in (artifact(created_at="invalid"), artifact(expired=None), artifact(workflow_run=None)):
            with self.subTest(value=value), self.assertRaises(guard.GuardError):
                self.decision(FakeClient({1: [value]}))

    def test_api_failure_propagates_instead_of_allowing_scoring(self):
        client = FakeClient()
        with patch.object(client, "get_json", side_effect=guard.GuardError("GitHub API request failed (HTTP 403).")):
            with self.assertRaises(guard.GuardError):
                self.decision(client)

    def test_outputs_written_once_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            summary = Path(directory) / "summary"
            result = self.decision(FakeClient(), verification_only=True)
            guard.write_decision(result, output, summary)
            text = output.read_text(encoding="utf-8")
            self.assertEqual(text.count("should_run="), 1)
            self.assertIn("should_run=false\nscore_date=" + TODAY, text)
            self.assertIn("verification only", summary.read_text(encoding="utf-8"))

    def test_main_api_failure_leaks_neither_token_nor_exception_body(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            env = {"GITHUB_REPOSITORY": "hhhhhades/ncmp", "GH_TOKEN": "sensitive-test-token", "GITHUB_OUTPUT": str(output)}
            error_log = io.StringIO()
            with patch.object(guard.GitHubClient, "get_json", side_effect=RuntimeError("sensitive-test-token response body")):
                with contextlib.redirect_stderr(error_log):
                    self.assertEqual(guard.main(env, now=NOW), 1)
            self.assertFalse(output.exists())
            self.assertNotIn("sensitive-test-token", error_log.getvalue())
            self.assertNotIn("response body", error_log.getvalue())


class GitHubClientTests(unittest.TestCase):
    def test_fixed_origin_and_safe_headers(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self, size):
                return b'{"artifacts": []}'

        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()

        opener = Opener()
        client = guard.GitHubClient("hhhhhades/ncmp", "test-token", opener)
        client.get_json(client.repository_path + "/actions/artifacts?per_page=100")
        self.assertEqual(urlparse(opener.request.full_url).netloc, "api.github.com")
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer test-token")
        with self.assertRaises(guard.GuardError):
            client.get_json("https://example.invalid/token")

    def test_http_errors_are_sanitized(self):
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 403, "test-token private body", {}, io.BytesIO(b"private body"))

        client = guard.GitHubClient("hhhhhades/ncmp", "test-token", Opener())
        with self.assertRaises(guard.GuardError) as result:
            client.get_json(client.repository_path + "/actions/artifacts")
        self.assertEqual(str(result.exception), "GitHub API request failed (HTTP 403).")

    def test_network_errors_are_sanitized(self):
        class Opener:
            def open(self, request, timeout):
                raise URLError("test-token private body")

        client = guard.GitHubClient("hhhhhades/ncmp", "test-token", Opener())
        with self.assertRaises(guard.GuardError) as result:
            client.get_json(client.repository_path + "/actions/artifacts")
        self.assertNotIn("test-token", str(result.exception))

    def test_invalid_repository_rejected(self):
        for repository in ("https://evil.invalid/repo", "owner/../evil", "owner/..", "owner/repo\n"):
            with self.subTest(repository=repository), self.assertRaises(guard.GuardError):
                guard.GitHubClient(repository, "test-token")


if __name__ == "__main__":
    unittest.main()
