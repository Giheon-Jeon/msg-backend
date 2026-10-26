from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_alter_team_team_score"),
    ]

    operations = [
        migrations.AddField(
            model_name="team",
            name="koth_division",
            field=models.CharField(
                choices=[("INTERNAL", "내부"), ("EXTERNAL", "외부")],
                default="EXTERNAL",
                db_default="EXTERNAL",
                max_length=10,
            ),
        ),
    ]
