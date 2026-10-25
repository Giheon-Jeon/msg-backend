"""Register a reviewed, pinned VM baseline; subsequent edits use administrator APIs."""
import json
import re
import sys
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.koth.environment import EnvironmentError, canonical_uuid, create_version, decode_settings, validate_settings
from apps.koth.environment_views import audit
from apps.koth.models import KothChallenge, KothEnvironmentApply, KothEnvironmentConfig


class Command(BaseCommand):
    help = "Register one reviewed KOTH baseline from JSON on stdin without printing values"

    def add_arguments(self, parser):
        parser.add_argument("--actor", required=True)

    def handle(self, *args, **options):
        try:
            raw = sys.stdin.read(2 * 1024 * 1024 + 1)
            body = json.loads(raw)
            if (len(raw.encode()) > 2 * 1024 * 1024 or not isinstance(body, dict)
                    or set(body) != {"challenge_id", "source_revision", "target_id", "agent_token_hash", "settings"}
                    or not re.fullmatch(r"[0-9a-f]{64}", body["agent_token_hash"])
                    or not re.fullmatch(r"gcp:[0-9]{1,25}", body["target_id"])
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}", options["actor"])):
                raise ValueError()
            challenge_id = canonical_uuid(body["challenge_id"])
            validate_settings(body["settings"])
        except (ValueError, TypeError, KeyError, EnvironmentError):
            raise CommandError("Reviewed baseline JSON is invalid") from None
        with transaction.atomic():
            challenge = KothChallenge.objects.select_for_update().filter(pk=challenge_id).first()
            if not challenge or challenge.source_revision != body["source_revision"] or challenge.status == "ACTIVE":
                raise CommandError("Challenge source or status does not match the reviewed baseline")
            current = KothEnvironmentConfig.objects.select_related("application__version").filter(challenge=challenge).first()
            if current:
                if (current.target_id != body["target_id"] or current.agent_token_hash != body["agent_token_hash"]
                        or not current.application or decode_settings(current.application.version) != body["settings"]):
                    raise CommandError("Existing environment differs; use the administrator version workflow")
                self.stdout.write(json.dumps({"challenge_id":str(challenge_id), "created":False, "version_id":str(current.application.version_id)}))
                return
            version = create_version(challenge, body["settings"], body["source_revision"], options["actor"], uuid.uuid4(), "0" * 64)
            application = KothEnvironmentApply.objects.create(application_id=uuid.uuid4(), challenge=challenge,
                version=version, request_fingerprint="0" * 64, requested_by=options["actor"])
            KothEnvironmentConfig.objects.create(challenge=challenge, target_id=body["target_id"],
                agent_token_hash=body["agent_token_hash"], application=application, apply_actor=options["actor"], state="QUEUED")
            audit(challenge, options["actor"], "운영 설정 버전 1 등록")
            self.stdout.write(json.dumps({"challenge_id":str(challenge_id), "created":True, "version_id":str(version.pk)}))
