import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase, override_settings


class AIAuthenticationTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hash_path = Path(self.directory.name) / 'api-key.sha256'
        self.key = 'plai_' + 'test-credential-' * 3
        self.hash_path.write_text(hashlib.sha256(self.key.encode()).hexdigest())
        self.settings_override = override_settings(
            PERMITLIFY_AI_KEY_HASH_FILE=self.hash_path,
            SECURE_SSL_REDIRECT=False,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def request(self, authorization='', **kwargs):
        return self.client.get(
            '/_internal/ai-auth/', HTTP_AUTHORIZATION=authorization, **kwargs
        )

    def test_valid_bearer_key_is_authorized_without_database_access(self):
        response = self.request(f'Bearer {self.key}')
        self.assertEqual(response.status_code, 204)
        self.assertIn('no-store', response['Cache-Control'])

    def test_invalid_credentials_are_rejected_without_echoing_them(self):
        for header in ('', 'Bearer', 'Bearer wrong-key', f'Basic {self.key}',
                       f'Bearer {self.key} extra', 'Bearer ' + 'x' * 4096,
                       'Bearer non-ascii-\u00e9'):
            with self.subTest(header=header[:30]):
                response = self.request(header)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()['error']['type'], 'authentication_error')
                self.assertIn('Bearer', response['WWW-Authenticate'])
                self.assertNotIn(self.key, response.content.decode())
                self.assertIn('no-store', response['Cache-Control'])

    def test_bearer_scheme_is_case_insensitive(self):
        self.assertEqual(self.request(f'bearer {self.key}').status_code, 204)

    def test_query_string_key_does_not_authenticate(self):
        response = self.client.get('/_internal/ai-auth/', {'api_key': self.key})
        self.assertEqual(response.status_code, 401)

    def test_missing_hash_fails_closed(self):
        self.hash_path.unlink()
        self.assertEqual(self.request(f'Bearer {self.key}').status_code, 503)

    def test_malformed_hash_fails_closed(self):
        for value in ('', 'g' * 64, '0' * 63, self.key):
            with self.subTest(value=value[:10]):
                self.hash_path.write_text(value)
                self.assertEqual(self.request(f'Bearer {self.key}').status_code, 503)

    def test_rotation_revokes_previous_key_immediately(self):
        replacement = 'replacement-test-credential'
        self.hash_path.write_text(hashlib.sha256(replacement.encode()).hexdigest())
        self.assertEqual(self.request(f'Bearer {self.key}').status_code, 401)
        self.assertEqual(self.request(f'Bearer {replacement}').status_code, 204)
