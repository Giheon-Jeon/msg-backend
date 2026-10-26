from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.instances.image_preparations import (
    PreparationError,
    configuration,
    process_preparation,
)
from apps.instances.models import ImagePreparationRequest


class Command(BaseCommand):
    help = "Deliver/poll a bounded batch of committed image preparation requests."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--check-config", action="store_true", help="Validate opt-in configuration only; no network or work is performed.")

    def handle(self, *args, **options):
        if options["check_config"]:
            try:
                configuration()
            except PreparationError as exc:
                raise CommandError(exc.code) from None
            self.stdout.write("Image preparation configuration valid.")
            return
        if not 1 <= options["limit"] <= 100:
            raise CommandError("limit must be between 1 and 100")
        if not settings.IMAGE_PREPARATION_ENABLED:
            self.stdout.write("Image preparation disabled; no requests sent.")
            return
        try:
            configuration()
            ids = list(
                ImagePreparationRequest.objects.filter(
                    next_attempt_at__lte=timezone.now()
                )
                .order_by("next_attempt_at", "request_id")
                .values_list("pk", flat=True)[: options["limit"]]
            )
            processed = sum(process_preparation(request_id) for request_id in ids)
        except PreparationError as exc:
            raise CommandError(exc.code) from None
        self.stdout.write(f"Processed {processed} image preparation requests.")
