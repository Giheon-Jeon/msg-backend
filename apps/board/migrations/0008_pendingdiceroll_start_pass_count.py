from django.db import migrations, models


def preserve_existing_start_passes(apps, schema_editor):
    PendingDiceRoll = apps.get_model("board", "PendingDiceRoll")
    PendingDiceRoll.objects.filter(passed_start=True).update(start_pass_count=1)


class Migration(migrations.Migration):
    dependencies = [
        ("board", "0007_cell_line_number_matches_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="pendingdiceroll",
            name="start_pass_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.RunPython(
            preserve_existing_start_passes,
            migrations.RunPython.noop,
        ),
    ]
