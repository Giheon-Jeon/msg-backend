from django.db import migrations


BOARD_LINES = (
    (2, 3, 4, 5, 6),
    (8, 9, 10, 11, 12),
    (13, 14, 15, 16, 17, 18),
    (19, 20, 21, 22, 23, 24),
    (26, 27, 28, 29, 30, 31),
    (32, 33, 34, 35, 36),
)


def assign_correct_line_numbers(apps, schema_editor):
    Cell = apps.get_model("board", "Cell")
    cells = Cell.objects.using(schema_editor.connection.alias)

    cells.update(line_number=None)
    for line_number, cell_indexes in enumerate(BOARD_LINES, start=1):
        cells.filter(cell_index__in=cell_indexes).update(line_number=line_number)


def restore_challenge_only_line_numbers(apps, schema_editor):
    Cell = apps.get_model("board", "Cell")
    cells = Cell.objects.using(schema_editor.connection.alias)

    cells.update(line_number=None)
    for line_number, cell_indexes in enumerate(BOARD_LINES, start=1):
        cells.filter(
            cell_index__in=cell_indexes,
            type="CHALLENGE",
        ).update(line_number=line_number)


class Migration(migrations.Migration):
    dependencies = [("board", "0008_pendingdiceroll_start_pass_count")]

    operations = [
        migrations.RemoveConstraint(
            model_name="cell",
            name="cell_line_number_matches_type",
        ),
        migrations.RunPython(
            assign_correct_line_numbers,
            restore_challenge_only_line_numbers,
        ),
    ]
