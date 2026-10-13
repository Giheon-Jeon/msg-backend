from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("board", "0006_cell_line_number")]

    operations = [
        migrations.AddConstraint(
            model_name="cell",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(line_number__isnull=True) | models.Q(type="CHALLENGE")
                ),
                name="cell_line_number_matches_type",
            ),
        ),
    ]
