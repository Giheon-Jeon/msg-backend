from django.db import migrations
from django.db.models import Count


def exclude_start_from_completion(apps, schema_editor):
    alias = schema_editor.connection.alias
    Consumption = apps.get_model("board", "TeamCellConsumption")
    State = apps.get_model("board", "TeamBoardState")

    # START remains consumed after its first landing. Only completion excludes it.
    completed_teams = (
        Consumption.objects.using(alias)
        .filter(cell_id__gte=2, cell_id__lte=36)
        .values("team_id")
        .annotate(consumed_count=Count("cell_id"))
        .filter(consumed_count=35)
        .values("team_id")
    )
    State.objects.using(alias).filter(team_id__in=completed_teams).update(
        next_dice_reset_at=None
    )


class Migration(migrations.Migration):
    dependencies = [("board", "0004_align_board_api_spec")]

    operations = [
        migrations.RunPython(exclude_start_from_completion, migrations.RunPython.noop),
    ]
