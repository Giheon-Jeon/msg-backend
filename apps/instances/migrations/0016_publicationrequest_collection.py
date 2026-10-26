from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0015_publication_dispatch_tracking")]

    operations = [
        migrations.AddField(
            model_name="publicationrequest",
            name="collection",
            field=models.JSONField(default=dict, blank=True),
        ),
    ]
