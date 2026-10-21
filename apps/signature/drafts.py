"""Create editable, unanswerable drafts for the six event clubs."""
from django.contrib.auth.hashers import is_password_usable, make_password
from django.db import transaction

from apps.koth.models import KothClub
from apps.signature.models import SignatureChallenge

EVENT_CLUBS = ("Aegis", "CodeCure", "MJSEC", "SeKurity", "SWING", "Y-CERT")


def missing_signature_fields(challenge):
    missing = []
    if not challenge.title.strip():
        missing.append("title")
    if not challenge.description.strip():
        missing.append("description")
    if not challenge.flag_hash or not is_password_usable(challenge.flag_hash):
        missing.append("flag")
    return missing


@transaction.atomic
def ensure_signature_drafts():
    rows = []
    for name in EVENT_CLUBS:
        club, club_created = KothClub.objects.get_or_create(name=name)
        challenge, created = SignatureChallenge.objects.get_or_create(club=club, defaults={
            "title": f"{name} 시그니처", "description": "", "flag_hash": make_password(None),
            "score": 300, "is_published": False,
        })
        rows.append({"club_name": name, "club_created": club_created, "signature_created": created,
                     "signature_id": str(challenge.pk)})
    return rows
