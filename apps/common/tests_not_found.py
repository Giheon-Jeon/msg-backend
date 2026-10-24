from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User


@override_settings(SECURE_SSL_REDIRECT=False)
class ApiRouteNotFoundTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def assert_json_not_found(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            response.json(),
            {"code": "NOT_FOUND", "message": "대상을 찾을 수 없습니다", "data": None},
        )

    def test_malformed_challenge_and_signature_ids_return_json(self):
        paths = (
            "/api/v1/admin/challenges/not-a-uuid",
            "/api/v1/admin/signatures/not-a-uuid",
            "/api/v1/challenges/not-a-uuid",
            "/api/v1/signatures/not-a-uuid",
            "/api/v1/challenges/not-a-uuid/submit",
            "/api/v1/signatures/not-a-uuid/submit",
        )
        for debug in (False, True):
            with override_settings(DEBUG=debug):
                for path in paths:
                    for method in ("get", "post", "delete"):
                        with self.subTest(debug=debug, path=path, method=method):
                            self.assert_json_not_found(getattr(self.client, method)(path))

    def test_unknown_api_routes_return_json_even_when_html_is_requested(self):
        for debug in (False, True):
            with override_settings(DEBUG=debug):
                for path in (
                    "/api", "/api/", "/api/v1",
                    "/api/v1/unknown?secret=hidden", "/api/v1/unknown%0Apath",
                ):
                    with self.subTest(debug=debug, path=path):
                        response = self.client.get(path, HTTP_ACCEPT="text/html")
                        self.assert_json_not_found(response)

    def test_head_unknown_api_route_keeps_json_headers_without_body(self):
        response = self.client.head("/api/v1/unknown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.content, b"")

    def test_unknown_api_writes_return_json_without_csrf_token(self):
        client = APIClient(enforce_csrf_checks=True)
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assert_json_not_found(getattr(client, method)("/api/v1/unknown"))

    def test_non_api_routes_keep_html_404(self):
        for debug in (False, True):
            with override_settings(DEBUG=debug):
                for path in ("/unknown", "/apiary/unknown"):
                    with self.subTest(debug=debug, path=path):
                        response = self.client.get(path)
                        self.assertEqual(response.status_code, 404)
                        self.assertTrue(response["Content-Type"].startswith("text/html"))

    def test_admin_login_keeps_html_response(self):
        response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/html"))

    def test_matching_api_routes_still_require_authentication(self):
        item_id = "11111111-1111-4111-8111-111111111111"
        for resource in ("challenges", "signatures"):
            with self.subTest(resource=resource):
                response = self.client.delete(f"/api/v1/admin/{resource}/{item_id}")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "TOKEN_MISSING")


@override_settings(SECURE_SSL_REDIRECT=False)
class ResolvedApiErrorTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            login_id="not-found-admin",
            nickname="not-found-admin",
            role=Role.ADMIN,
            team=Team.objects.create(team_name="not-found-team"),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=user)

    def test_valid_missing_ids_keep_resource_specific_errors(self):
        item_id = "11111111-1111-4111-8111-111111111111"
        for resource, code in (
            ("challenges", "CHALLENGE_NOT_FOUND"),
            ("signatures", "SIGNATURE_NOT_FOUND"),
        ):
            with self.subTest(resource=resource):
                response = self.client.delete(f"/api/v1/admin/{resource}/{item_id}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["code"], code)
                self.assertIsNone(response.json()["data"])

    def test_matching_routes_keep_method_not_allowed_errors(self):
        response = self.client.delete("/api/v1/auth/login")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "METHOD_NOT_ALLOWED")
