import datetime

from django.db import migrations


def migrate_opened_challenges(apps, schema_editor):
    OpenedChallenge = apps.get_model("challenge", "OpenedChallenge")
    Solve = apps.get_model("challenge", "Solve")
    Cell = apps.get_model("board", "Cell")
    TeamChallengeAccess = apps.get_model("board", "TeamChallengeAccess")
    database = schema_editor.connection.alias

    # Every access must remain readable after the legacy table is removed.
    # Check before any updates, including accesses already present on the board.
    missing_challenge_ids = set(
        OpenedChallenge.objects.using(database)
        .filter(challenge__board_meta__isnull=True)
        .values_list("challenge_id", flat=True)
    )
    missing_challenge_ids.update(
        TeamChallengeAccess.objects.using(database)
        .filter(challenge__board_meta__isnull=True)
        .values_list("challenge_id", flat=True)
    )
    if missing_challenge_ids:
        identifiers = ", ".join(sorted(str(pk) for pk in missing_challenge_ids))
        raise RuntimeError(
            "Cannot migrate opened challenges: BoardChallenge metadata is missing "
            f"for {len(missing_challenge_ids)} challenge(s): {identifiers}. "
            "Restore their verified challenge_number and club_name, then retry. "
            "Do not delete or skip the existing access records."
        )

    for opened in (
        OpenedChallenge.objects.using(database).order_by("opened_at").iterator()
    ):
        solve = (
            Solve.objects.using(database)
            .filter(
                team_id=opened.team_id,
                challenge_id=opened.challenge_id,
            )
            .first()
        )
        existing_access = (
            TeamChallengeAccess.objects.using(database)
            .filter(
                team_id=opened.team_id,
                challenge_id=opened.challenge_id,
            )
            .first()
        )
        if existing_access is not None:
            # Keep the canonical board access and its opening time, but carry
            # over completed solves before removing the legacy access table.
            if solve is not None:
                TeamChallengeAccess.objects.using(database).filter(
                    pk=existing_access.pk
                ).update(
                    status="CLEARED",
                    cleared_at=solve.solved_at,
                )
            continue

        if not Cell.objects.using(database).filter(pk=opened.cell_index).exists():
            raise RuntimeError(
                "Cannot migrate opened challenge "
                f"{opened.pk}: board cell {opened.cell_index} does not exist."
            )

        conflicting_access = (
            TeamChallengeAccess.objects.using(database)
            .filter(
                team_id=opened.team_id,
                source_cell_id=opened.cell_index,
            )
            .first()
        )
        if conflicting_access is not None:
            raise RuntimeError(
                "Cannot migrate opened challenge "
                f"{opened.pk}: team {opened.team_id} already has a different challenge "
                f"for board cell {opened.cell_index}."
            )

        access = TeamChallengeAccess.objects.using(database).create(
            team_id=opened.team_id,
            challenge_id=opened.challenge_id,
            source_cell_id=opened.cell_index,
            status="CLEARED" if solve is not None else "OPENED",
            cleared_at=solve.solved_at if solve is not None else None,
        )
        TeamChallengeAccess.objects.using(database).filter(pk=access.pk).update(
            opened_at=opened.opened_at
        )


def restore_opened_challenges(apps, schema_editor):
    OpenedChallenge = apps.get_model("challenge", "OpenedChallenge")
    TeamChallengeAccess = apps.get_model("board", "TeamChallengeAccess")
    database = schema_editor.connection.alias

    for access in (
        TeamChallengeAccess.objects.using(database).order_by("opened_at").iterator()
    ):
        if (
            OpenedChallenge.objects.using(database)
            .filter(
                team_id=access.team_id,
                challenge_id=access.challenge_id,
            )
            .exists()
        ):
            continue

        opened = OpenedChallenge.objects.using(database).create(
            team_id=access.team_id,
            challenge_id=access.challenge_id,
            cell_index=access.source_cell_id,
            solve_deadline_at=access.opened_at + datetime.timedelta(minutes=15),
        )
        OpenedChallenge.objects.using(database).filter(pk=opened.pk).update(
            opened_at=access.opened_at
        )


class Migration(migrations.Migration):
    dependencies = [
        ("board", "0002_idempotencyrequest"),
        ("challenge", "0003_alter_challenge_category"),
    ]

    operations = [
        migrations.RunPython(migrate_opened_challenges, restore_opened_challenges),
        migrations.DeleteModel(name="OpenedChallenge"),
    ]
