import json
import os
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent

sys.path.insert(0, str(TOOL_DIR))
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo_settings")

import django

django.setup()

from django.core.management import call_command

from apps.accounts.models import Role, Team, User
from apps.challenge.models import Challenge
from apps.challenge.services import hash_flag

CATEGORY_MAP = {
    "web": Challenge.CategoryType.WEB,
    "pwn": Challenge.CategoryType.PWN,
    "rev": Challenge.CategoryType.REV,
    "crypto": Challenge.CategoryType.CRYPTO,
    "forensic": Challenge.CategoryType.FORENSIC,
    "koth": Challenge.CategoryType.MISC,
    "misc": Challenge.CategoryType.MISC,
}


def main():
    call_command("migrate", verbosity=0)

    if not User.objects.filter(login_id="root").exists():
        team = Team.objects.create(team_name="데모팀")
        User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=team
        )
        User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )

    with open(TOOL_DIR / "catalog.json", encoding="utf-8") as source:
        catalog = json.load(source)

    resolved = []
    for entry in catalog:
        challenge = Challenge.objects.filter(title=entry["title"]).first()
        if challenge is None:
            challenge = Challenge.objects.create(
                title=entry["title"],
                category=CATEGORY_MAP[entry["category"].lower()],
                difficulty=Challenge.DifficultyType.MEDIUM,
                score=700,
                description=entry["slug"] + " 데모 문제",
                flag_hash=hash_flag(entry["flag"]),
                is_published=True,
            )
        row = dict(entry)
        row["challenge_id"] = str(challenge.challenge_id)
        resolved.append(row)

    with open(TOOL_DIR / ".data" / "catalog_resolved.json", "w", encoding="utf-8") as out:
        json.dump(resolved, out, ensure_ascii=False, indent=2)

    print("시드 완료: 계정 root, player (pw1234) / 문제", len(resolved), "개")
    print("다음: fake_scheduler.py, runserver, demo_server.py 순서로 실행")


if __name__ == "__main__":
    main()
