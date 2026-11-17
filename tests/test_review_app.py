import unittest
from unittest.mock import patch


class ReviewAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('sms._open', side_effect=AssertionError('No provider traffic allowed')):
            from review_app import app
        cls.client = app.test_client()

    def test_preview_and_all_scenarios_render_without_raw_jinja(self):
        for path in ['/', '/scenario/complete', '/scenario/partial', '/scenario/interrupted', '/scenario/booking']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Simulation only', response.data)
            self.assertNotIn(b'{{', response.data)
            self.assertNotIn(b'{%', response.data)

    def test_structured_outcomes_and_disabled_sms(self):
        data = self.client.get('/review.json').json
        self.assertFalse(data['sms_enabled'])
        self.assertFalse(data['live_ai'])
        self.assertEqual(data['scenarios']['complete']['outcome']['state']['status'], 'complete')
        for name in ['partial', 'interrupted', 'booking']:
            self.assertEqual(data['scenarios'][name]['outcome']['state']['status'], 'needs_human')

    def test_no_production_webhooks_login_or_write_routes(self):
        for path in ['/voice/incoming', '/voice/dial-result', '/sms/incoming', '/login']:
            self.assertEqual(self.client.post(path).status_code, 404)
        self.assertEqual(self.client.post('/').status_code, 405)
        self.assertEqual(self.client.get('/scenario/unknown').status_code, 404)

    def test_health_and_headers(self):
        response = self.client.get('/health')
        self.assertEqual(response.json['mode'], 'synthetic-review')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
