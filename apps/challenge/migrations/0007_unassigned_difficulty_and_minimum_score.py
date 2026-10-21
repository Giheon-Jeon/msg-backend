from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("challenge", "0006_merge_20260912_1928")]

    operations = [
        migrations.AlterField(
            model_name="challenge", name="difficulty",
            field=models.CharField(max_length=20, choices=[("UNASSIGNED", "미정"), ("EASY", "Easy"), ("MEDIUM", "Medium"), ("HARD", "Hard")]),
        ),
        migrations.AlterField(
            model_name="challenge", name="minimum_score",
            field=models.DecimalField(max_digits=12, decimal_places=2, default=100),
        ),
    ]
