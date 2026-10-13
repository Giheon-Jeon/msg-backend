from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase
from apps.timer.models import Contest
from django.db import IntegrityError
from datetime import datetime, timedelta, timezone as datetime_timezone


class TimerAPITest(APITestCase):
    def test_running_when_contest_in_progress(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=11),
            is_active=True,
        )

        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        self.assertEqual(response.data["data"]["status"], "RUNNING")

    def test_before_when_contest_not_started(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=13),
            is_active=True,
        )

        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "BEFORE")

    def test_ended_when_contest_finished(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=13),
            end_time=now - timedelta(hours=1),
            is_active=True,
        )

        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "ENDED")

    def test_null_data_when_no_active_contest(self):
        response = self.client.get("/api/v1/timer")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        self.assertIsNone(response.data["data"])

    def test_response_has_exact_fields(self):  # 응답 필드 전체 확인
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=11),
            is_active=True,
        )
        response = self.client.get("/api/v1/timer")
        self.assertEqual(
            set(response.data["data"].keys()),
            {
                "name",
                "status",
                "start_time",
                "end_time",
                "remaining_seconds",
                "remaining_display",
                "time_until_start",
                "server_time",
            },
        )

    # remaining_seconds와 remaining_display 확인
    def test_remaining_seconds_and_display_match(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=2),
            is_active=True,
        )
        data = self.client.get("/api/v1/timer").data["data"]
        self.assertAlmostEqual(data["remaining_seconds"], 7200, delta=5)
        self.assertRegex(data["remaining_display"], r"^01:59:\d{2}$")

    # RUNNING + 0초 해결
    def test_running_at_exact_end_time_is_ended(self):
        now = timezone.now()
        contest = Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=12),
            end_time=now + timedelta(hours=1),
            is_active=True,
        )
        snapshot = contest.snapshot(now=contest.end_time)
        self.assertEqual(snapshot["status"], "ENDED")
        self.assertEqual(snapshot["remaining_seconds"], 0)

    # 제약 위반 테스트
    def test_second_active_contest_rejected(self):
        now = timezone.now()
        Contest.objects.create(
            name="first",
            start_time=now,
            end_time=now + timedelta(hours=12),
            is_active=True,
        )
        with self.assertRaises(IntegrityError):
            Contest.objects.create(
                name="second",
                start_time=now,
                end_time=now + timedelta(hours=12),
                is_active=True,
            )

    def test_start_time_after_end_time_rejected(self):
        now = timezone.now()
        with self.assertRaises(IntegrityError):
            Contest.objects.create(
                name="invalid",
                start_time=now + timedelta(hours=1),
                end_time=now,
                is_active=False,
            )

    # BEFORE 상태에서 remaining_seconds와 remaining_display 값을 확인하는 테스트
    def test_before_returns_total_contest_duration(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=13),
            is_active=True,
        )
        data = self.client.get("/api/v1/timer").data["data"]
        self.assertEqual(data["remaining_seconds"], 43200)
        self.assertEqual(data["remaining_display"], "12:00:00")
        self.assertAlmostEqual(data["time_until_start"], 3600, delta=5)

    def test_server_time_matches_remaining(self):
        now = timezone.now()
        Contest.objects.create(
            name="test",
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=2),
            is_active=True,
        )
        data = self.client.get("/api/v1/timer").data["data"]

        self.assertRegex(data["server_time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

        server_time = datetime.strptime(data["server_time"], "%Y-%m-%dT%H:%M:%SZ")
        server_time = server_time.replace(tzinfo=datetime_timezone.utc)
        expected_end = server_time + timedelta(seconds=data["remaining_seconds"])
        self.assertAlmostEqual(
            expected_end.timestamp(),
            (now + timedelta(hours=2)).timestamp(),
            delta=5,
        )
