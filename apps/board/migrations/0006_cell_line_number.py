from django.db import migrations, models


BOARD_LINES = (
    (2, 3, 4, 5, 6),
    (8, 9, 10, 11, 12),
    (13, 14, 15, 17, 18),
    (19, 20, 22, 23, 24),
    (26, 27, 28, 29, 31),
    (32, 33, 34, 35, 36),
)


def assign_cell_line_numbers(apps, schema_editor):
    Cell = apps.get_model("board", "Cell")
    alias = schema_editor.connection.alias

    Cell.objects.using(alias).update(line_number=None)
    for line_number, cell_indexes in enumerate(BOARD_LINES, start=1):
        Cell.objects.using(alias).filter(
            cell_index__in=cell_indexes,
            type="CHALLENGE",
        ).update(line_number=line_number)


def clear_cell_line_numbers(apps, schema_editor):
    Cell = apps.get_model("board", "Cell")
    Cell.objects.using(schema_editor.connection.alias).update(line_number=None)


class Migration(migrations.Migration):
    dependencies = [("board", "0005_exclude_start_from_completion")]

    operations = [
        migrations.AddField(
            model_name="cell",
            name="line_number",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(assign_cell_line_numbers, clear_cell_line_numbers),
        migrations.AddConstraint(
            model_name="cell",
            constraint=models.CheckConstraint(
                condition=models.Q(line_number__isnull=True)
                | models.Q(line_number__gte=1, line_number__lte=6),
                name="cell_line_number_between_1_and_6",
            ),
        ),
    ]
