import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0007_challengerelease_approved_at_releasecontainer_env_and_more")]

    operations = [
        migrations.RemoveConstraint(
            model_name="challengerelease", name="uq_release_challenge_revision"
        ),
        migrations.AddField(
            model_name="challengerelease",
            name="derived_from",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="derived_releases", to="instances.challengerelease",
            ),
        ),
    ]
