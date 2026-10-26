import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("challenge", "0008_exesubmission")]

    operations = [
        migrations.AddField(
            model_name="exesubmission",
            name="checker_commit_sha",
            field=models.CharField(
                max_length=40,
                null=True,
                blank=True,
                validators=[
                    django.core.validators.RegexValidator(regex=r"\A[0-9a-f]{40}\Z")
                ],
            ),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="vm_template_sha256",
            field=models.CharField(
                max_length=64,
                null=True,
                blank=True,
                validators=[
                    django.core.validators.RegexValidator(regex=r"\A[0-9a-f]{64}\Z")
                ],
            ),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="driver_sha256",
            field=models.CharField(
                max_length=64,
                null=True,
                blank=True,
                validators=[
                    django.core.validators.RegexValidator(regex=r"\A[0-9a-f]{64}\Z")
                ],
            ),
        ),
        migrations.AddField(
            model_name="exesubmission",
            name="config_version",
            field=models.CharField(max_length=200, null=True, blank=True),
        ),
        migrations.AddConstraint(
            model_name="exesubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(checker_commit_sha__isnull=True)
                | models.Q(checker_commit_sha__regex=r"\A[0-9a-f]{40}\Z"),
                name="ck_exe_checker_sha",
            ),
        ),
        migrations.AddConstraint(
            model_name="exesubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(vm_template_sha256__isnull=True)
                | models.Q(vm_template_sha256__regex=r"\A[0-9a-f]{64}\Z"),
                name="ck_exe_vm_sha256",
            ),
        ),
        migrations.AddConstraint(
            model_name="exesubmission",
            constraint=models.CheckConstraint(
                condition=models.Q(driver_sha256__isnull=True)
                | models.Q(driver_sha256__regex=r"\A[0-9a-f]{64}\Z"),
                name="ck_exe_driver_sha256",
            ),
        ),
    ]
