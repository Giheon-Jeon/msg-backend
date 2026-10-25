import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("koth", "0007_environment_versions")]
    operations = [
        migrations.AddField(model_name="kothchallenge", name="metadata_revision", field=models.CharField(max_length=40, blank=True, default="")),
        migrations.AddField(model_name="kothchallenge", name="files_mode", field=models.CharField(max_length=20, default="REPOSITORY", choices=[("REPOSITORY", "REPOSITORY"), ("NONE", "NONE")])),
        migrations.AddField(model_name="kothchallenge", name="updated_by", field=models.CharField(max_length=50, blank=True, default="")),
        migrations.CreateModel(name="KothPublication", fields=[
            ("publication_id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ("registry_revision", models.PositiveBigIntegerField()),
            ("source_sha", models.CharField(max_length=40)),
            ("artifact_id", models.PositiveBigIntegerField(unique=True)),
            ("run_id", models.PositiveBigIntegerField()),
            ("fingerprint", models.CharField(max_length=64)),
            ("metadata", models.JSONField(default=dict)),
            ("runtime_profile", models.JSONField(default=dict)),
            ("missing", models.JSONField(default=list)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("created_by", models.CharField(max_length=50)),
            ("challenge", models.ForeignKey(to="koth.kothchallenge", on_delete=django.db.models.deletion.PROTECT, related_name="publications")),
        ], options={"db_table": "koth_publications", "ordering": ["-registry_revision"], "constraints": [models.UniqueConstraint(fields=("challenge", "registry_revision"), name="uq_koth_publication_revision")]}),
        migrations.AddField(model_name="kothenvironmentversion", name="publication", field=models.ForeignKey(to="koth.kothpublication", on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name="environment_versions")),
        migrations.AddField(model_name="kothchallenge", name="active_publication", field=models.ForeignKey(to="koth.kothpublication", on_delete=django.db.models.deletion.PROTECT, null=True, blank=True, related_name="active_challenges")),
    ]
