from concurrent.futures import ThreadPoolExecutor

from django.db import connections, transaction
from django.test import TransactionTestCase

from apps.board.catalog import ensure_board_challenge
from apps.board.models import BoardChallenge
from apps.challenge.models import Challenge


class CatalogEnrollmentConcurrencyTests(TransactionTestCase):
    def test_concurrent_publications_allocate_distinct_numbers_and_retry_reuses_them(self):
        challenges = [Challenge.objects.create(title=f"실제 문제 {index}", category="WEB", difficulty="EASY",
                                               score=1000, flag_hash="test-only") for index in range(12)]

        def publish(challenge_id):
            try:
                with transaction.atomic():
                    challenge = Challenge.objects.select_for_update().get(pk=challenge_id)
                    entry = ensure_board_challenge(challenge)
                    challenge.is_published = True
                    challenge.save(update_fields=["is_published"])
                    return str(entry.pk), entry.challenge_number
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=4) as pool:
            first = list(pool.map(publish, [item.pk for item in challenges]))
            second = list(pool.map(publish, [item.pk for item in challenges]))
        self.assertEqual(first, second)
        self.assertEqual(len({number for _, number in first}), 12)
        self.assertEqual(BoardChallenge.objects.count(), 12)
