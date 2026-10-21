"""Enroll a published challenge without renumbering the existing board catalog."""
from django.db import connection
from django.db.models import Max

from apps.board.models import BoardChallenge


def ensure_board_challenge(challenge):
    # The caller holds this challenge's row lock inside a transaction.
    existing = BoardChallenge.objects.filter(challenge=challenge).first()
    if existing is not None:
        return existing
    # Publications of different challenges need one allocator lock as well.
    # PostgreSQL releases it on either commit or rollback.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [20261020, 1])
    number = (BoardChallenge.objects.aggregate(last=Max("challenge_number"))["last"] or 0) + 1
    return BoardChallenge.objects.create(challenge=challenge, challenge_number=number, club_name="")
