"""Offline regression tests for truthful scoring receipts and safe retries.

Run: python -m unittest tests.test_scoring_result -v
These tests never contact NetEase, GitHub, or an email server.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import main as entrypoint
from src.core.bot import MusicPartnerBot
from src.core.signer import Signer, beijing_date
from src.core.tasks.daily import DailyTask
from src.core.tasks.extra import ExtraTask


def config(full=False):
    value = MagicMock()
    value.get.side_effect = lambda key, default=None: {
        "full_extra_tasks": full,
        "score": 3,
        "Cookie_MUSIC_U": "private-cookie",
        "Cookie___csrf": "private-csrf",
    }.get(key, default)
    value.get_wait_time.return_value = 0
    return value


def work(number):
    return {"id": number, "name": f"song-{number}", "authorName": "artist", "resourceId": number}


def task(number, completed=False):
    return {"work": work(number), "completed": completed, "score": 3}


def daily_data(done, total=2):
    return {
        "id": "daily-id", "count": total, "completedCount": done,
        "works": [task(i, i < done) for i in range(total)],
    }


def session():
    value = MagicMock()
    value.cookies = {"__csrf": "private-csrf"}
    return value


class DailyCompletionTests(unittest.TestCase):
    def setUp(self):
        self.subject = DailyTask(session(), MagicMock(), config())

    def test_already_completed_never_posts_scores(self):
        self.subject._get_daily_tasks = MagicMock(return_value=(True, daily_data(2)))
        with patch("src.core.tasks.daily.Signer") as signer:
            result = self.subject.process_and_verify()
        signer.assert_not_called()
        self.assertEqual(result["submitted"], 0)
        self.assertEqual(result["completed_after"], 2)

    def test_only_uncompleted_work_is_scored_and_server_confirms(self):
        self.subject._get_daily_tasks = MagicMock(side_effect=[
            (False, daily_data(1)), (True, daily_data(2)),
        ])
        with patch("src.core.tasks.daily.Signer") as signer:
            signer.return_value.sign.return_value = True
            result = self.subject.process_and_verify()
        self.assertEqual(signer.return_value.sign.call_args.args[0]["id"], 1)
        self.assertEqual(signer.return_value.sign.call_count, 1)
        self.assertEqual(result["submitted"], 1)

    def test_405_skip_is_not_daily_completion(self):
        self.subject._get_daily_tasks = MagicMock(return_value=(False, daily_data(1)))
        with patch("src.core.tasks.daily.Signer") as signer, patch("src.core.tasks.daily.time.sleep"):
            signer.return_value.sign.return_value = False
            with self.assertRaisesRegex(RuntimeError, "尚未确认"):
                self.subject.process_and_verify()
        self.assertIsNone(self.subject.result)

    def test_invalid_server_payload_is_not_zero_work_success(self):
        for payload in ({}, {"code": 500, "data": daily_data(2)}, {"code": 200, "data": {}}):
            with self.subTest(payload=payload):
                self.subject.session.get.return_value.json.return_value = payload
                with self.assertRaises(RuntimeError):
                    self.subject._get_daily_tasks()

    def test_explicit_empty_daily_assignment_is_valid(self):
        self.subject._get_daily_tasks = MagicMock(return_value=(True, daily_data(0, 0)))
        self.assertEqual(self.subject.process_and_verify()["total"], 0)

    def test_duplicate_daily_works_stop_before_scoring(self):
        payload = daily_data(0)
        payload["works"] = [task(1), task(1)]
        self.subject.session.get.return_value.json.return_value = {"code": 200, "data": payload}
        with patch("src.core.tasks.daily.Signer") as signer:
            with self.assertRaisesRegex(RuntimeError, "重复"):
                self.subject.process_and_verify()
        signer.assert_not_called()

    def test_changed_daily_assignment_cannot_be_confirmed_as_original(self):
        final = daily_data(2)
        final["id"] = "another-daily-id"
        self.subject._get_daily_tasks = MagicMock(side_effect=[(False, daily_data(1)), (True, final)])
        with patch("src.core.tasks.daily.Signer") as signer:
            signer.return_value.sign.return_value = True
            with self.assertRaisesRegex(RuntimeError, "ID 已改变"):
                self.subject.process_and_verify()
        self.assertIsNone(self.subject.result)


class ExtraCompletionTests(unittest.TestCase):
    def create(self, full=False):
        return ExtraTask(session(), MagicMock(), config(full))

    def test_seven_task_limit_and_existing_completions_are_preserved(self):
        subject = self.create()
        pending = [task(i) for i in range(10)]
        subject._get_extra_tasks = MagicMock(side_effect=[(pending, 2), (pending[5:], 7)])
        subject._process_single_task = MagicMock(return_value=True)
        result = subject.process_extra_tasks("daily-id")
        self.assertEqual(subject._process_single_task.call_count, 5)
        self.assertEqual(result["target"], 7)
        self.assertEqual(result["submitted"], 5)

    def test_full_extra_tasks_scores_the_entire_available_assignment(self):
        subject = self.create(full=True)
        subject._get_extra_tasks = MagicMock(side_effect=[
            ([task(i) for i in range(9)], 2), ([], 11),
        ])
        subject._process_single_task = MagicMock(return_value=True)
        result = subject.process_extra_tasks("daily-id")
        self.assertEqual(subject._process_single_task.call_count, 9)
        self.assertTrue(result["full_extra_tasks"])
        self.assertEqual(result["target"], 11)

    def test_partial_failure_cannot_become_completed_receipt(self):
        subject = self.create(full=True)
        pending = [task(1), task(2)]
        subject._get_extra_tasks = MagicMock(side_effect=[
            (pending, 0), ([task(2)], 1), ([task(2)], 1), ([task(2)], 1),
        ])
        subject._process_single_task = MagicMock(side_effect=[True, RuntimeError("rejected")])
        with patch("src.core.tasks.extra.time.sleep"), self.assertRaisesRegex(RuntimeError, "1/2"):
            subject.process_extra_tasks("daily-id")

    def test_skipped_resource_is_not_counted_as_submitted(self):
        subject = self.create(full=True)
        subject._get_extra_tasks = MagicMock(return_value=([task(1)], 0))
        subject._process_single_task = MagicMock(return_value=False)
        with patch("src.core.tasks.extra.time.sleep"), self.assertRaisesRegex(RuntimeError, "提交 0"):
            subject.process_extra_tasks("daily-id")

    def test_post_acknowledgement_requires_server_completion(self):
        subject = self.create(full=True)
        subject._get_extra_tasks = MagicMock(return_value=([task(1)], 0))
        subject._process_single_task = MagicMock(return_value=True)
        with patch("src.core.tasks.extra.time.sleep"), self.assertRaises(RuntimeError):
            subject.process_extra_tasks("daily-id")

    def test_server_already_completed_and_no_assignment_are_legitimate(self):
        for pending, completed in (([], 9), ([], 0)):
            with self.subTest(completed=completed):
                subject = self.create()
                subject._get_extra_tasks = MagicMock(return_value=(pending, completed))
                subject._process_single_task = MagicMock()
                result = subject.process_extra_tasks("daily-id")
                subject._process_single_task.assert_not_called()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["submitted"], 0)

    def test_duplicate_work_ids_are_rejected(self):
        subject = self.create(full=True)
        subject.session.get.return_value.json.return_value = {"code": 200, "data": [task(1), task(1)]}
        with self.assertRaisesRegex(RuntimeError, "重复"):
            subject._get_extra_tasks()


class ScoringSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.subject = Signer(session(), "daily-id", MagicMock(), config())
        self.subject._get_params = MagicMock(return_value="encrypted")
        self.subject._get_enc_sec_key = MagicMock(return_value="encrypted-key")

    def test_resource_state_skip_is_explicit_false(self):
        self.subject.session.post.return_value.json.return_value = {"code": 405, "message": "资源状态异常"}
        self.assertFalse(self.subject.sign(work(1)))
        self.subject.logger.debug.assert_not_called()

    def test_uncertain_post_is_not_retried_or_logged_with_csrf(self):
        self.subject.session.post.side_effect = requests.Timeout("https://invalid/?csrf_token=private-csrf")
        with self.assertRaises(RuntimeError):
            self.subject.sign(work(1))
        self.assertEqual(self.subject.session.post.call_count, 1)
        self.assertNotIn("private-csrf", str(self.subject.logger.mock_calls))

    def test_frequency_retries_are_bounded(self):
        self.subject.session.post.return_value.json.return_value = {"code": 429, "message": "评分频繁"}
        with self.assertRaises(RuntimeError):
            self.subject.sign(work(1))
        self.assertEqual(self.subject.session.post.call_count, 3)

    def test_date_change_during_configured_wait_prevents_post(self):
        today = self.subject.score_date
        with patch("src.core.signer.beijing_date", side_effect=[today, "2000-01-01"]):
            with self.assertRaisesRegex(RuntimeError, "日期已改变"):
                self.subject.sign(work(1))
        self.subject.session.post.assert_not_called()

    def test_existing_score_strategy_and_wait_are_used(self):
        self.subject.session.post.return_value.json.return_value = {"code": 200}
        self.assertTrue(self.subject.sign(work(1), is_extra=True))
        data = self.subject._get_params.call_args.args[0]
        self.assertEqual(data["score"], "4")
        self.assertEqual(data["tags"], "4-A-1")
        self.assertEqual(data["extraResource"], "true")
        self.subject.config.get_wait_time.assert_called_once()


class ReceiptTests(unittest.TestCase):
    def run_main(self, path, success=True, cookie_valid=True, target=None, crossing=False):
        bot = MagicMock()
        bot.run.return_value = success
        bot.result = {
            "daily": {"status": "completed", "total": 5, "completed_after": 5},
            "extra": {"status": "completed", "target": 7, "completed_after": 7},
        }
        environment = {"NCMP_RESULT_FILE": str(path), "NCMP_SCORE_DATE": target or beijing_date()}
        with (
            patch.dict(os.environ, environment),
            patch.object(entrypoint, "Config", return_value=config()) as config_mock,
            patch.object(entrypoint, "Logger"),
            patch.object(entrypoint, "NotificationService") as notify,
            patch.object(entrypoint, "CookieValidator") as validator,
            patch.object(entrypoint, "MusicPartnerBot", return_value=bot),
        ):
            validator.return_value.validate.return_value = (cookie_valid, "cookie invalid")
            if crossing:
                with patch.object(entrypoint, "ensure_score_date", side_effect=[None, None, RuntimeError("date changed")]):
                    code = entrypoint.main()
            else:
                code = entrypoint.main()
        return code, bot, config_mock, notify

    def test_only_confirmed_success_emits_secret_free_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "score.json"
            code, _, _, _ = self.run_main(path)
            self.assertEqual(code, 0)
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["schema"], 1)
            self.assertEqual(receipt["status"], "completed")
            self.assertEqual(receipt["score_date"], beijing_date())
            self.assertIn("started_at", receipt)
            self.assertIn("completed_at", receipt)
            self.assertNotIn("private", path.read_text(encoding="utf-8"))

    def test_failed_scoring_returns_nonzero_and_removes_stale_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "score.json"
            path.write_text('{"status":"completed"}', encoding="utf-8")
            code, _, _, notify = self.run_main(path, success=False)
            self.assertEqual(code, 1)
            self.assertFalse(path.exists())
            notify.return_value.send_notification.assert_called_once()

    def test_cookie_failure_and_date_change_do_not_create_receipts(self):
        for kwargs in ({"cookie_valid": False}, {"crossing": True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "score.json"
                code, _, _, _ = self.run_main(path, **kwargs)
                self.assertEqual(code, 1)
                self.assertFalse(path.exists())

    def test_stale_target_date_stops_before_configuration_or_scoring(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "score.json"
            code, bot, configuration, _ = self.run_main(path, target="2000-01-01")
            self.assertEqual(code, 1)
            configuration.assert_not_called()
            bot.run.assert_not_called()
            self.assertFalse(path.exists())

    def test_network_error_redacts_sensitive_query_string(self):
        with patch.object(requests.Session, "request", side_effect=requests.Timeout("url?csrf_token=private")):
            with self.assertRaises(RuntimeError) as error:
                entrypoint.TimeoutSession().get("https://invalid/")
        self.assertNotIn("private", str(error.exception))
        self.assertIn("Timeout", str(error.exception))


class BotFailureTests(unittest.TestCase):
    def test_incomplete_daily_prevents_extra_tasks_and_result(self):
        bot = MusicPartnerBot(config(), MagicMock(), session())
        bot._verify_user = MagicMock()
        with patch("src.core.bot.DailyTask") as daily, patch("src.core.bot.ExtraTask") as extra:
            daily.return_value.process_and_verify.side_effect = RuntimeError("incomplete daily")
            self.assertFalse(bot.run())
            extra.assert_not_called()
        self.assertIsNone(bot.result)


if __name__ == "__main__":
    unittest.main()
