import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("instances", "0014_publicationrequest")]

    operations = [
        migrations.AlterField(
            model_name="publicationrequest",
            name="dispatch_status",
            field=models.CharField(
                max_length=20,
                default="NOT_DISPATCHED",
                choices=[
                    ("NOT_DISPATCHED", "Not Dispatched"),
                    ("DISPATCH_UNKNOWN", "Dispatch Unknown"),
                    ("DISPATCHED", "Dispatched"),
                    ("DISPATCH_REJECTED", "Dispatch Rejected"),
                    ("DISPATCH_CONFLICT", "Dispatch Conflict"),
                ],
            ),
        ),
        migrations.AddField(
            model_name="publicationrequest",
            name="dispatch_started_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="publicationrequest",
            name="last_checked_at",
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="publicationrequest",
            name="last_error_code",
            field=models.CharField(max_length=60, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="publicationrequest",
            name="run_head_sha",
            field=models.CharField(max_length=40, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="publicationrequest",
            name="run_number",
            field=models.PositiveIntegerField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name="publicationrequest",
            name="reused_request",
            field=models.ForeignKey(
                "instances.publicationrequest",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="reused_by",
                null=True,
                blank=True,
            ),
        ),
    ]
