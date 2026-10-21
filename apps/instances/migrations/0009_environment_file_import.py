from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0008_challengerelease_derived_from")]

    operations = [
        migrations.AddField(
            model_name="releasecontainer", name="env_rules",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="challengerelease", name="env_import_id",
            field=models.UUIDField(null=True, blank=True, unique=True, editable=False),
        ),
        migrations.AddField(
            model_name="challengerelease", name="env_import_fingerprint",
            field=models.CharField(max_length=64, blank=True, default="", editable=False),
        ),
    ]
