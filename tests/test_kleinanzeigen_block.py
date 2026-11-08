import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from user_jobs import kleinanzeigen_block as kb

BASE = 20 * 60


def resp(code):
    return SimpleNamespace(status_code=code)


class KleinanzeigenBlockTests(unittest.TestCase):
    def setUp(self):
        kb.reset()
        self.addCleanup(kb.reset)
        sleep = mock.patch('user_jobs.kleinanzeigen_block.time.sleep')
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def test_a_normal_answer_goes_straight_through(self):
        kb.begin_run()
        with mock.patch('requests.get', return_value=resp(200)) as get:
            self.assertEqual(kb.get('u', timeout=1).status_code, 200)
        get.assert_called_once()
        self.sleep.assert_not_called()

    def test_a_403_is_retried_once_after_a_pause(self):
        kb.begin_run()
        with mock.patch('requests.get', side_effect=[resp(403), resp(200)]) as get:
            self.assertEqual(kb.get('u', timeout=1).status_code, 200)
        self.assertEqual(get.call_count, 2)
        self.sleep.assert_called_once_with(kb.RETRY_AFTER_SECONDS)

    def test_a_second_403_goes_through_the_pc_proxy_for_the_rest_of_the_run(self):
        kb.begin_run()
        with mock.patch.object(kb, 'PROXY', 'socks5h://host.docker.internal:1080'), \
             mock.patch('requests.get', side_effect=[resp(403), resp(403), resp(200), resp(200)]) as get:
            self.assertEqual(kb.get('u', timeout=1).status_code, 200)
            self.assertEqual(kb.get('v', timeout=1).status_code, 200)
        self.assertNotIn('proxies', get.call_args_list[0].kwargs)
        self.assertEqual(get.call_args_list[2].kwargs['proxies']['https'], 'socks5h://host.docker.internal:1080')
        self.assertIn('proxies', get.call_args_list[3].kwargs)

    def test_without_a_proxy_the_blocked_answer_is_returned_for_raise_for_status(self):
        kb.begin_run()
        with mock.patch.object(kb, 'PROXY', ''), mock.patch('requests.get', side_effect=[resp(403), resp(403)]):
            self.assertEqual(kb.get('u', timeout=1).status_code, 403)

    def test_a_blocked_run_doubles_the_interval_up_to_an_hour_and_skips_runs_in_between(self):
        now = datetime(2026, 11, 8, 18, 31, tzinfo=timezone.utc)
        for expected in (1, 2, 2):
            self.assertTrue(kb.begin_run(now))
            kb._state['run']['blocked'] = True
            kb.end_run(True, BASE, now)
            self.assertEqual(kb.level(), expected)
            self.assertFalse(kb.begin_run(now + timedelta(minutes=20)))
            now += timedelta(minutes=20 * (expected + 1))
        self.assertEqual(kb.interval_minutes(BASE), 60)

    def test_calm_runs_bring_the_interval_back_step_by_step(self):
        now = datetime(2026, 11, 8, 18, 31, tzinfo=timezone.utc)
        kb.begin_run(now)
        kb._state['run']['blocked'] = True
        kb.end_run(True, BASE, now)
        self.assertEqual(kb.level(), 1)
        for _ in range(kb.CALM_RUNS_TO_STEP_DOWN):
            now += timedelta(minutes=40)
            self.assertTrue(kb.begin_run(now))
            kb.end_run(True, BASE, now)
        self.assertEqual(kb.level(), 0)
        self.assertTrue(kb.begin_run(now + timedelta(minutes=20)))

    def test_alert_only_after_two_hours_of_failing_runs(self):
        now = datetime(2026, 11, 8, 18, 31, tzinfo=timezone.utc)
        kb.begin_run(now)
        kb.end_run(False, BASE, now)
        self.assertFalse(kb.failing_too_long(now + timedelta(minutes=90)))
        self.assertTrue(kb.failing_too_long(now + timedelta(hours=2)))
        kb.begin_run(now + timedelta(hours=3))
        kb.end_run(True, BASE, now + timedelta(hours=3))
        self.assertFalse(kb.failing_too_long(now + timedelta(hours=4)))

    def test_the_evening_summary_counts_blocks_and_proxy_results(self):
        kb.begin_run()
        with mock.patch.object(kb, 'PROXY', 'socks5h://x:1080'), \
             mock.patch('requests.get', side_effect=[resp(403), resp(403), resp(200)]):
            kb.get('u', timeout=1)
        kb.end_run(True, BASE)
        until = datetime.now(timezone.utc) + timedelta(seconds=1)
        lines = kb.summary_lines(until - timedelta(days=1), until, BASE)
        self.assertEqual(len(lines), 1)
        self.assertIn('відмов 403/429 — <b>1</b>', lines[0])
        self.assertIn('через проксі ПК пройшло 1', lines[0])
        self.assertIn('інтервал зараз 40 хв', lines[0])

    def test_a_quiet_day_adds_nothing_to_the_summary(self):
        until = datetime.utcnow()
        self.assertEqual(kb.summary_lines(until - timedelta(days=1), until, BASE), [])


if __name__ == '__main__':
    unittest.main()
