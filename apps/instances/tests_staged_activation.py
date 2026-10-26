import copy
import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch
from io import StringIO

from django.db import close_old_connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.core.management import call_command, CommandError
from django.utils import timezone

from apps.adminpanel.models import AdminEvent
from apps.instances.image_preparations import _call_runtime, _validate_result, configuration, process_preparation, queue_retry, serialize_preparation
from apps.instances.models import ChallengeRelease, ChallengeRuntimeConfig, ImagePreparationRequest
from apps.instances import tests_image_preparations as preparation_tests
from apps.instances.tests_image_preparations import CONFIG, accepted, detail
from apps.instances.tests_releases import ReleaseTestBase


class PreparationInstallationConfigurationTests(SimpleTestCase):
    @override_settings(**CONFIG)
    def test_installer_configuration_check_never_contacts_runtime_or_database(self):
        call_command('process_image_preparations', check_config=True, stdout=StringIO())

    @override_settings(IMAGE_PREPARATION_ENABLED=False)
    def test_installer_configuration_check_rejects_disabled_feature(self):
        with self.assertRaises(CommandError):
            call_command('process_image_preparations', check_config=True, stdout=StringIO())

    @override_settings(**CONFIG)
    @patch('apps.instances.image_preparations.build_opener')
    def test_readiness_transport_accepts_200_post_response(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.status, response.read.return_value = 200, b'{"targets":[]}'
        self.assertEqual(_call_runtime(configuration(), '/v1/image-readiness/query', {'architecture': 'AMD64'}), {'targets': []})
        response.status = 202
        with self.assertRaises(ValueError):
            _call_runtime(configuration(), '/v1/image-readiness/query', {})


@override_settings(**CONFIG, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=True)
class StagedActivationApiTests(ReleaseTestBase):
    def setUp(self):
        super().setUp()
        self.auth('root')
        with override_settings(IMAGE_PREPARATION_ENABLED=False, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=False):
            self.old = self.register().data['data']['release_id']
            self.activate(self.old)
        self.new = self.register(revision=2).data['data']['release_id']

    def test_activation_approves_and_durably_queues_without_switch_or_http(self):
        with patch('apps.instances.image_preparations._call_runtime', side_effect=AssertionError('request must not use HTTP')):
            response = self.client.post(f'{self.base_url}/{self.new}/activate', {'reason': 'staged rollout'}, format='json')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(str(ChallengeRuntimeConfig.objects.get().current_release_id), self.old)
        row = ImagePreparationRequest.objects.get()
        self.assertEqual(response.data['data']['preparation']['request_id'], str(row.pk))
        self.assertEqual(row.activation_status, 'PENDING')
        self.assertEqual(row.activation_reason, 'staged rollout')
        self.assertEqual(row.created_by, 'root')
        self.assertIsNotNone(ChallengeRelease.objects.get(pk=self.new).approved_at)
        self.assertEqual(AdminEvent.objects.filter(type='CHALLENGE_RELEASE_ACTIVATED').count(), 1)

    def test_repeated_pending_activation_returns_same_request_and_generation(self):
        first = self.activate(self.new)
        second = self.activate(self.new)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.data['data']['preparation']['request_id'], second.data['data']['preparation']['request_id'])
        self.assertEqual(ImagePreparationRequest.objects.count(), 1)

    def test_later_activation_supersedes_pending_even_when_current_is_requested(self):
        self.assertEqual(self.activate(self.new).status_code, 202)
        self.assertEqual(self.activate(self.old).status_code, 200)
        self.assertEqual(ImagePreparationRequest.objects.get().activation_status, 'SUPERSEDED')
        self.assertEqual(str(ChallengeRuntimeConfig.objects.get().current_release_id), self.old)

    def test_invalid_preparation_configuration_rolls_back_approval_and_pending_state(self):
        with override_settings(IMAGE_PREPARATION_TARGET_IDS=''):
            self.assertEqual(self.activate(self.new).status_code, 503)
        self.assertIsNone(ChallengeRelease.objects.get(pk=self.new).approved_at)
        self.assertFalse(ImagePreparationRequest.objects.exists())
        self.assertEqual(str(ChallengeRuntimeConfig.objects.get().current_release_id), self.old)

    def test_outer_transaction_rollback_removes_pending_activation_and_approval(self):
        with transaction.atomic():
            self.assertEqual(self.activate(self.new).status_code, 202)
            transaction.set_rollback(True)
        self.assertFalse(ImagePreparationRequest.objects.exists())
        self.assertIsNone(ChallengeRelease.objects.get(pk=self.new).approved_at)
        self.assertEqual(str(ChallengeRuntimeConfig.objects.get().current_release_id), self.old)

    def test_unsupported_windows_and_koth_cannot_enter_staged_activation(self):
        for values in ({'runtime_type': 'DOCKER', 'architecture': 'AMD64', 'isolation_profile': 'WINDOWS'},
                       {'runtime_type': 'KUBERNETES', 'architecture': 'AMD64', 'isolation_profile': 'KOTH'}):
            ChallengeRelease.objects.filter(pk=self.new).update(**values)
            self.assertEqual(self.activate(self.new).status_code, 400)
            self.assertEqual(str(ChallengeRuntimeConfig.objects.get().current_release_id), self.old)
        self.assertFalse(ImagePreparationRequest.objects.exists())


def readiness(row):
    now = timezone.now()
    return {
        'image_set_id': row.expected_image_set_id,
        'architecture': 'AMD64',
        'targets': [
            {'target_id': target, 'ready': True, 'ready_node_count': 2, 'eligible_node_count': 2,
             'observed_at': (now - timedelta(seconds=1)).isoformat(),
             'valid_until': (now + timedelta(seconds=30)).isoformat()}
            for target in ('target-a', 'target-b')
        ],
    }


@override_settings(**CONFIG, IMAGE_PREPARATION_ACTIVATE_WHEN_READY=True, IMAGE_PREPARATION_READINESS_MAX_AGE_SECONDS=60)
class StagedActivationWorkerTests(TransactionTestCase):
    setUp = preparation_tests.PreparationWorkerTests.setUp
    due = preparation_tests.PreparationWorkerTests.due
    run_once = preparation_tests.PreparationWorkerTests.run_once
    accept = preparation_tests.PreparationWorkerTests.accept

    def stage(self):
        self.config = ChallengeRuntimeConfig.objects.create(challenge=self.challenge, activation_generation=1)
        self.row.activation_status = 'PENDING'
        self.row.activation_generation = 1
        self.row.activation_reason = 'ready rollout'
        self.row.save()
        self.accept()
        self.run_once(detail(self.row))
        self.assertIsNotNone(self.row.next_attempt_at)

    def test_success_history_does_not_switch_until_fresh_current_query(self):
        self.stage()
        self.config.refresh_from_db()
        self.assertIsNone(self.config.current_release_id)
        self.run_once(readiness(self.row))
        self.config.refresh_from_db()
        self.assertEqual(self.config.current_release_id, self.release.pk)
        self.assertEqual(self.row.activation_status, 'ACTIVATED')
        event = AdminEvent.objects.get(type='CHALLENGE_RELEASE_ACTIVATED')
        self.assertEqual(event.actor, 'root')
        self.assertIn('ready rollout', event.message)
        self.assertIsNone(self.row.next_attempt_at)
        self.assertFalse(process_preparation(self.row.pk))
        self.assertEqual(AdminEvent.objects.filter(type='CHALLENGE_RELEASE_ACTIVATED').count(), 1)
        path, payload = self.remote.call_args.args[1:]
        self.assertEqual(path, '/v1/image-readiness/query')
        self.assertEqual(payload, {'architecture': 'AMD64', 'images': [
            'ghcr.io/msg-ctf/challenges/web-basic/app@sha256:' + 'a' * 64,
            'ghcr.io/msg-ctf/challenges/web-basic/db@sha256:' + 'b' * 64,
        ], 'target_ids': ['target-a', 'target-b']})

    def test_partial_missing_stale_future_and_mismatched_evidence_never_switches(self):
        self.stage()
        good = readiness(self.row)
        changes = [
            lambda d: d['targets'].pop(),
            lambda d: d['targets'][0].update(ready_node_count=1),
            lambda d: d['targets'][0].update(ready=False),
            lambda d: d['targets'][0].update(eligible_node_count=0, ready_node_count=0),
            lambda d: d['targets'][0].update(ready_node_count=True),
            lambda d: d['targets'][0].update(observed_at=(timezone.now()+timedelta(seconds=10)).isoformat()),
            lambda d: d['targets'][0].update(observed_at=(timezone.now()-timedelta(seconds=61)).isoformat()),
            lambda d: d['targets'][0].update(valid_until=(timezone.now()-timedelta(seconds=1)).isoformat()),
            lambda d: d['targets'][0].update(observed_at=None, valid_until=None),
            lambda d: d.update(architecture='ARM64'),
            lambda d: d.update(image_set_id='sha256:' + '0' * 64),
            lambda d: d['targets'].append(copy.deepcopy(d['targets'][0])),
        ]
        for mutate in changes:
            with self.subTest(mutate=mutate):
                self.row.activation_status = 'PENDING'
                self.row.consecutive_failures = 0
                self.row.save()
                data = copy.deepcopy(good)
                mutate(data)
                self.run_once(data)
                self.config.refresh_from_db()
                self.assertIsNone(self.config.current_release_id)
                self.assertTrue(self.row.last_error_code)
        self.assertFalse(AdminEvent.objects.filter(type='CHALLENGE_RELEASE_ACTIVATED').exists())

    def test_not_ready_exhausts_bounded_attempts_and_keeps_old_pointer(self):
        self.stage()
        data = readiness(self.row)
        data['targets'][0]['ready'] = False
        for _ in range(3):
            self.run_once(data)
        self.assertEqual(self.row.activation_status, 'FAILED')
        self.assertIsNone(self.row.next_attempt_at)
        self.assertEqual(self.row.last_error_code, 'IMAGE_READINESS_NOT_READY')

    def test_later_operator_generation_cannot_be_overwritten_after_http(self):
        self.stage()
        def answer(*args):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            ChallengeRuntimeConfig.objects.filter(pk=self.config.pk).update(activation_generation=2)
            return readiness(self.row)
        self.due()
        self.remote.side_effect = answer
        process_preparation(self.row.pk)
        self.row.refresh_from_db()
        self.config.refresh_from_db()
        self.assertEqual(self.row.activation_status, 'SUPERSEDED')
        self.assertIsNone(self.config.current_release_id)

    def test_configuration_or_policy_switch_disables_pending_activation(self):
        self.stage()
        for changed in ({'IMAGE_PREPARATION_TARGET_IDS': 'target-a,target-b,target-c'},
                        {'IMAGE_PREPARATION_ACTIVATE_WHEN_READY': False}):
            self.row.activation_status = 'PENDING'
            self.row.save()
            with self.subTest(changed=changed), override_settings(**changed):
                self.due()
                process_preparation(self.row.pk)
                self.row.refresh_from_db()
                self.assertEqual(self.row.activation_status, 'FAILED')
                self.assertIsNone(self.row.next_attempt_at)

    def test_explicit_failed_node_retry_retains_activation_intent_and_parent_history(self):
        self.config = ChallengeRuntimeConfig.objects.create(challenge=self.challenge, activation_generation=1)
        self.row.activation_status = 'PENDING'
        self.row.activation_generation = 1
        self.row.activation_reason = 'retry rollout'
        self.row.save()
        self.accept()
        self.run_once(detail(self.row, status='PARTIAL', states=('READY', 'FAILED')))
        self.assertEqual(self.row.activation_status, 'FAILED')
        child = queue_retry(self.row, 'retry-admin')
        self.assertEqual(child.activation_status, 'PENDING')
        self.assertEqual(child.activation_generation, 1)
        self.assertEqual(child.activation_reason, 'retry rollout')
        self.row.refresh_from_db()
        self.assertEqual(self.row.operation_status, 'PARTIAL')
        encoded = str(serialize_preparation(child))
        for secret in ('hidden-env', 'unit-test-preparation-token', 'secret_env'):
            self.assertNotIn(secret, encoded)
        self.run_once(accepted(child, operation_id='prep-retry'), row=child)
        result = detail(child)
        result['nodes'][0]['node_uid'] = 'node-1'
        self.run_once(result, row=child)
        self.assertEqual(child.activation_status, 'PENDING')
        self.run_once(readiness(child), row=child)
        self.assertEqual(child.activation_status, 'ACTIVATED')
        self.config.refresh_from_db()
        self.assertEqual(self.config.current_release_id, self.release.pk)
        self.row.refresh_from_db()
        self.assertEqual(self.row.activation_status, 'FAILED')
        self.assertEqual(self.row.operation_status, 'PARTIAL')
        event = AdminEvent.objects.get(type='CHALLENGE_RELEASE_ACTIVATED')
        self.assertEqual(event.actor, 'retry-admin')
        self.assertIn('retry rollout', event.message)

    def test_response_loss_restart_recovers_readiness_lease_and_commits_once(self):
        self.stage()
        self.row.delivery_status = 'SENDING'
        self.row.next_attempt_at = timezone.now() + timedelta(seconds=30)
        self.row.save()
        self.assertFalse(process_preparation(self.row.pk))
        self.run_once(readiness(self.row))
        self.assertEqual(self.row.activation_status, 'ACTIVATED')
        self.assertFalse(process_preparation(self.row.pk))
        self.assertEqual(AdminEvent.objects.filter(type='CHALLENGE_RELEASE_ACTIVATED').count(), 1)

    def test_audit_failure_rolls_back_pointer_and_restart_rechecks_live_readiness(self):
        self.stage()
        with patch('apps.instances.admin_views._record_release_switch', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.run_once(readiness(self.row))
        self.config.refresh_from_db()
        self.assertIsNone(self.config.current_release_id)
        self.row.refresh_from_db()
        self.assertEqual(self.row.activation_status, 'PENDING')
        self.run_once(readiness(self.row))
        self.assertEqual(self.row.activation_status, 'ACTIVATED')
        self.assertEqual(AdminEvent.objects.filter(type='CHALLENGE_RELEASE_ACTIVATED').count(), 1)

    def test_only_one_worker_queries_readiness_during_a_live_lease(self):
        self.stage()
        self.due()
        entered, release = Event(), Event()
        def answer(*args):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            entered.set()
            self.assertTrue(release.wait(5))
            return readiness(self.row)
        def worker():
            close_old_connections()
            try:
                return process_preparation(self.row.pk)
            finally:
                close_old_connections()
        self.remote.side_effect = answer
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            self.assertTrue(entered.wait(5))
            try:
                self.assertFalse(pool.submit(worker).result(timeout=5))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=5))
        self.row.refresh_from_db()
        self.assertEqual(self.row.activation_status, 'ACTIVATED')
        self.assertEqual(AdminEvent.objects.filter(type='CHALLENGE_RELEASE_ACTIVATED').count(), 1)

    def test_expired_evidence_after_waiting_for_switch_lock_never_activates(self):
        self.stage()
        data = readiness(self.row)
        for target in data['targets']:
            target['valid_until'] = (timezone.now()+timedelta(milliseconds=50)).isoformat()
        from apps.instances.image_preparations import _complete_activation
        def delayed(*args):
            # Expiration is rechecked by the production function after obtaining locks.
            with patch('apps.instances.image_preparations.timezone.now', return_value=timezone.now()+timedelta(seconds=1)):
                return _complete_activation(*args)
        with patch('apps.instances.image_preparations._complete_activation', side_effect=delayed):
            self.run_once(data)
        self.assertNotEqual(self.row.activation_status, 'ACTIVATED')
        self.config.refresh_from_db()
        self.assertIsNone(self.config.current_release_id)

    def test_settings_disabled_during_readiness_response_records_failure(self):
        self.stage()
        def answer(*args):
            self.override = override_settings(IMAGE_PREPARATION_ENABLED=False)
            self.override.enable()
            self.addCleanup(self.override.disable)
            return readiness(self.row)
        self.due()
        self.remote.side_effect = answer
        self.assertTrue(process_preparation(self.row.pk))
        self.row.refresh_from_db()
        self.assertEqual(self.row.activation_status, 'FAILED')
        self.assertEqual(self.row.last_error_code, 'IMAGE_PREPARATION_DISABLED')

    def test_release_snapshot_change_during_query_never_activates_stale_images(self):
        self.stage()
        def answer(*args):
            self.release.containers.filter(name='db').update(
                image_ref='ghcr.io/msg-ctf/challenges/web-basic/db@sha256:' + 'c' * 64)
            return readiness(self.row)
        self.due()
        self.remote.side_effect = answer
        self.assertTrue(process_preparation(self.row.pk))
        self.row.refresh_from_db()
        self.config.refresh_from_db()
        self.assertEqual(self.row.activation_status, 'FAILED')
        self.assertEqual(self.row.last_error_code, 'IMAGE_PREPARATION_RELEASE_CHANGED')
        self.assertIsNone(self.config.current_release_id)

    def test_runtime_finite_agent_error_codes_remain_classified_without_raw_messages(self):
        for code in ('NETWORK_TIMEOUT', 'REGISTRY_UNAVAILABLE', 'RATE_LIMITED', 'REGISTRY_UNAUTHORIZED',
                     'MANIFEST_NOT_FOUND', 'PLATFORM_MISMATCH', 'RESOURCE_EXHAUSTED',
                     'NODE_IDENTITY_MISMATCH', 'AGENT_UNAVAILABLE', 'CONTENT_INCOMPLETE', 'POLICY_REJECTED'):
            with self.subTest(code=code):
                data = detail(self.row, status='FAILED', states=('FAILED',))
                data['error_code'] = code
                data['message'] = 'registry-password-must-never-escape'
                for image in data['nodes'][0]['images']:
                    image['error_code'] = code
                    image['message'] = data['message']
                _, _, progress = _validate_result(self.row, data, True)
                self.assertEqual(progress['error_code'], code)
                self.assertEqual(progress['nodes'][0]['images'][0]['error_code'], code)
                self.assertNotIn(data['message'], json.dumps(progress))
