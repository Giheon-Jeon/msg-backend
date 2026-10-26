from django.db import migrations, models


LEGACY_POINTS = {1: 40, 2: 25, 3: 15, 4: 12, 5: 8}


def backfill_legacy_award_snapshots(apps, schema_editor):
    Team = apps.get_model("accounts", "Team")
    KothScorePeriod = apps.get_model("koth", "KothScorePeriod")

    for period in KothScorePeriod.objects.filter(status="APPLIED").iterator():
        payload = (period.response_payload or {}).get("data") or {}
        results = payload.get("results") or []
        result_ids = [
            item.get("team_id") for item in results if item.get("team_id")
        ]
        teams = {
            str(team.pk): team
            for team in Team.objects.filter(pk__in=result_ids)
        }
        grouped = {}
        awarded_scores = {
            str(item.get("team_id")): 0
            for item in results
            if item.get("team_id")
        }
        for item in results:
            team_id = str(item.get("team_id") or "")
            rank = item.get("period_rank", item.get("rank"))
            if team_id not in teams or isinstance(rank, bool) or not isinstance(rank, int):
                continue
            grouped.setdefault(rank, []).append(team_id)

        for rank, team_ids in grouped.items():
            occupied_points = sum(
                LEGACY_POINTS.get(position, 0)
                for position in range(rank, rank + len(team_ids))
            )
            amount = occupied_points // len(team_ids)
            if amount > 0:
                for team_id in team_ids:
                    awarded_scores[team_id] = amount

        period.award_snapshot = {
            "version": 1,
            "algorithm": "fixed_top_5_v1",
            "provenance": "RECONSTRUCTED_FROM_RESPONSE",
            "points": list(LEGACY_POINTS.values()),
            "team_divisions": {
                team_id: team.koth_division for team_id, team in teams.items()
            },
            "awarded_scores": awarded_scores,
        }
        period.save(update_fields=["award_snapshot"])


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0003_team_koth_division"),
        ("koth", "0009_alter_kothsolve_earned_score"),
    ]

    operations = [
        migrations.AddField(
            model_name="kothscoreperiod",
            name="award_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(
            backfill_legacy_award_snapshots,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
