import uuid

from django.db import migrations, models
from django.contrib.postgres.functions import RandomUUID


def assign_confirmation_ids(apps, schema_editor):
    pending_rolls = apps.get_model("board", "PendingDiceRoll")
    for pending in pending_rolls.objects.using(schema_editor.connection.alias).iterator():
        pending.confirmation_id = uuid.uuid4()
        pending.save(update_fields=["confirmation_id"], using=schema_editor.connection.alias)


class Migration(migrations.Migration):
    dependencies = [("board", "0010_dicerollrequest")]

    operations = [
        migrations.AddField(
            model_name="pendingdiceroll",
            name="confirmation_id",
            field=models.UUIDField(null=True, editable=False),
        ),
        migrations.RunPython(assign_confirmation_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="pendingdiceroll",
            name="confirmation_id",
            field=models.UUIDField(
                default=uuid.uuid4, db_default=RandomUUID(), unique=True, editable=False
            ),
        ),
    ]
