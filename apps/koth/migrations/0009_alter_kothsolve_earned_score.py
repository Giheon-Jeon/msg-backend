from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("koth", "0008_publications"),
    ]

    operations = [
        migrations.AlterField(
            model_name="kothsolve",
            name="earned_score",
            field=models.PositiveBigIntegerField(default=0),
        ),
    ]
