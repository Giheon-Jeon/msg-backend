from django.db import migrations, models
from django.db.models import F


def backfill_positive_score_solved_at(apps, schema_editor):
    KothSolve = apps.get_model("koth", "KothSolve")
    KothSolve.objects.filter(
        earned_score__gt=0,
        solved_at__isnull=True,
    ).update(solved_at=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [
        ("koth", "0003_one_challenge_per_club"),
    ]

    operations = [
        migrations.RunPython(
            backfill_positive_score_solved_at,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="kothsolve",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(earned_score__lte=0) | models.Q(solved_at__isnull=False)
                ),
                name="ck_koth_positive_score_has_solved_at",
            ),
        ),
    ]
