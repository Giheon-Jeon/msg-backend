from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("challenge", "0010_remote_run_config_and_events")]

    operations = [
        migrations.AddField(
            model_name="exesubmission",
            name="cleanup_confirmed_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
    ]
