from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("adminpanel", "0010_merge_signature_and_submission_events"),
        ("adminpanel", "0010_resource_account_billing"),
    ]

    operations = [
        migrations.AlterField(
            model_name="adminevent",
            name="type",
            field=models.CharField(
                choices=[
                    ("TEAM_BANNED", "Team Banned"),
                    ("TEAM_UNBANNED", "Team Unbanned"),
                    ("MILEAGE_ADJUSTED", "Mileage Adjusted"),
                    ("PAYMENT_REFUNDED", "Payment Refunded"),
                    ("INSTANCE_FAILED", "Instance Failed"),
                    ("INSTANCE_FORCED", "Instance Forced"),
                    ("CHALLENGE_VISIBILITY_CHANGED", "Challenge Visibility Changed"),
                    ("SETTINGS_CHANGED", "Settings Changed"),
                    ("DICE_ADJUSTED", "Dice Adjusted"),
                    ("BOARD_POSITION_MOVED", "Board Position Moved"),
                    ("CELL_STATUS_CHANGED", "Cell Status Changed"),
                    ("TEAM_UPDATED", "Team Updated"),
                    ("TEAM_DELETED", "Team Deleted"),
                    ("ACCOUNT_CREATED", "Account Created"),
                    ("PAYMENT_PROCESSED", "Payment Processed"),
                    ("CHALLENGE_CREATED", "Challenge Created"),
                    ("CHALLENGE_UPDATED", "Challenge Updated"),
                    ("LINE_MONOPOLIZED", "Line Monopolized"),
                    ("LINE_MONOPOLY_SCORE_CHANGED", "Line Monopoly Score Changed"),
                    ("CHALLENGE_RELEASE_ACTIVATED", "Challenge Release Activated"),
                    ("SIGNATURE_CREATED", "Signature Created"),
                    ("SIGNATURE_UPDATED", "Signature Updated"),
                    ("SIGNATURE_DELETED", "Signature Deleted"),
                    ("SUBMISSION_DELETED", "Submission Deleted"),
                    ("RESOURCE_ACCOUNT_UPDATED", "Resource Account Updated"),
                ],
                max_length=40,
            ),
        ),
    ]
