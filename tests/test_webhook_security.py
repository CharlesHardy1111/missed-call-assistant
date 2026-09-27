import base64
import hashlib
import hmac
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from werkzeug.datastructures import MultiDict
from test_app import flask_create_app
from database import get_calls
from signed_client import TEST_ORIGIN, TEST_TOKEN, TEST_EVENT_TOKEN


def signature(url, data, token=TEST_TOKEN):
    # Independent implementation in tests, not the application's SDK validator.
    params = MultiDict(data)
    value = url + ''.join(key + item for key in sorted(params)
                          for item in sorted(set(params.getlist(key))))
    return base64.b64encode(hmac.new(token.encode(), value.encode(), hashlib.sha1).digest()).decode()


class WebhookSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = flask_create_app({
            "TESTING": True, "ENABLE_SMS_FOLLOWUP": False,
            "DATABASE_PATH": str(Path(self.temp.name) / 'security.db'),
            "BUSINESS_PHONE": "+15555550100", "ENABLE_DEV_ROUTES": True,
            "TWILIO_AUTH_TOKEN": TEST_TOKEN,
            "TWILIO_WEBHOOK_BASE_URL": TEST_ORIGIN,
            "CALL_EVENT_TOKEN": TEST_EVENT_TOKEN,
        })
        # Deliberately use the ordinary client; it never adds authentication.
        self.client = self.app.test_client()
        patcher = patch("sms._open", side_effect=AssertionError("Network forbidden"))
        self.network = patcher.start()
        self.addCleanup(patcher.stop)

    def calls(self):
        with self.app.app_context():
            return get_calls()

    def payloads(self):
        return {
            "/voice/incoming": {"From": "+15555550101", "CallSid": "parent"},
            "/voice/dial-result": {"From": "+15555550101", "CallSid": "parent", "DialCallStatus": "no-answer"},
            "/provider/call-status": {"From": "+15555550101", "CallSid": "parent", "CallStatus": "no-answer"},
            "/voice/trial-test": {"From": "+15555550101"},
        }

    def signed_post(self, path, data, **kwargs):
        headers = kwargs.pop("headers", {})
        headers["X-Twilio-Signature"] = signature(TEST_ORIGIN + path, data)
        return self.client.post(path, data=data, headers=headers,
                                content_type="application/x-www-form-urlencoded", **kwargs)

    def test_valid_signatures_preserve_forwarding_and_missed_call_flow(self):
        for path, data in self.payloads().items():
            response = self.signed_post(path, data)
            self.assertIn(response.status_code, (200, 201))
            if path == "/voice/incoming":
                dial = ElementTree.fromstring(response.data).find('Dial')
                self.assertEqual(dial.attrib, {"action": "/voice/dial-result", "method": "POST", "timeout": "20"})
                self.assertEqual(dial.find('Number').text, "+15555550100")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]['follow_up_status'], 'disabled')
        self.network.assert_not_called()

    def test_missing_and_invalid_signatures_cannot_touch_leads_or_sms(self):
        with patch('followup.send_sms') as send:
            for path, data in self.payloads().items():
                for invalid in (None, '', 'wrong', 'A' * 27 + '=', 'not-ascii-\u00e9'):
                    with self.subTest(path=path, invalid=invalid):
                        headers = {} if invalid is None else {'X-Twilio-Signature': invalid}
                        self.assertEqual(self.client.post(path, data=data, headers=headers).status_code, 403)
            send.assert_not_called()
        self.assertEqual(self.calls(), [])

    def test_body_and_url_tampering_are_rejected(self):
        path = '/voice/dial-result'
        data = self.payloads()[path]
        sig = signature(TEST_ORIGIN + path, data)
        for changed in ({**data, 'From': '+15555550109'}, {**data, 'DialCallStatus': 'busy'}, {**data, 'Extra': 'new'}):
            self.assertEqual(self.client.post(path, data=changed, headers={'X-Twilio-Signature': sig}).status_code, 403)
        self.assertEqual(self.client.post(path + '?extra=1', data=data, headers={'X-Twilio-Signature': sig}).status_code, 403)
        self.assertEqual(self.client.post('/voice/incoming', data=data, headers={'X-Twilio-Signature': sig}).status_code, 403)
        self.assertEqual(self.calls(), [])

    def test_all_parameters_and_exact_query_encoding_are_validated(self):
        path = '/voice/incoming?value=a%2Bb&second=x%20y'
        data = MultiDict([('From', '+15555550101'), ('FutureField', 'z'), ('FutureField', 'a')])
        self.assertEqual(self.signed_post(path, data).status_code, 200)
        headers = {'X-Twilio-Signature': signature(TEST_ORIGIN + path, data)}
        self.assertEqual(self.client.post(path.replace('%2B', '+'), data=data, headers=headers).status_code, 403)
        changed = MultiDict([('From', '+15555550101'), ('FutureField', 'z'), ('FutureField', 'b')])
        self.assertEqual(self.client.post(path, data=changed, headers=headers).status_code, 403)

    def test_proxy_internal_http_does_not_break_valid_public_https_signature(self):
        path = '/voice/incoming'
        data = self.payloads()[path]
        self.assertEqual(self.signed_post(path, data, base_url='http://internal:10000', headers={
            'X-Forwarded-Host': 'attacker.invalid', 'X-Forwarded-Proto': 'http',
            'Forwarded': 'host=attacker.invalid;proto=http',
        }).status_code, 200)

    def test_spoofed_host_and_forwarded_headers_cannot_select_signing_url(self):
        path = '/voice/dial-result'
        data = self.payloads()[path]
        headers = {'X-Twilio-Signature': signature('https://attacker.invalid' + path, data),
                   'Host': 'attacker.invalid', 'X-Forwarded-Host': 'attacker.invalid',
                   'X-Forwarded-Proto': 'https'}
        self.assertEqual(self.client.post(path, data=data, headers=headers).status_code, 403)
        self.assertEqual(self.calls(), [])

    def test_missing_credentials_fail_closed_even_in_testing_or_debug(self):
        self.app.config['DEBUG'] = True
        for key in ('TWILIO_AUTH_TOKEN', 'TWILIO_WEBHOOK_BASE_URL'):
            original = self.app.config[key]
            self.app.config[key] = ''
            for path, data in self.payloads().items():
                self.assertEqual(self.signed_post(path, data).status_code, 503)
            self.app.config[key] = original
        self.assertEqual(self.calls(), [])

    def test_invalid_origin_config_fails_closed(self):
        for origin in ('http://public.example', 'https://user:password@example.test',
                       'https://example.test/path', 'https://example.test?query=1',
                       'https://example.test#fragment', 'https://example.test:invalid', ''):
            self.app.config['TWILIO_WEBHOOK_BASE_URL'] = origin
            self.assertEqual(self.signed_post('/voice/incoming', {}).status_code, 503)

    def test_local_loopback_still_requires_real_signature_validation(self):
        origin = 'http://127.0.0.1:5000'
        self.app.config['TWILIO_WEBHOOK_BASE_URL'] = origin
        path = '/voice/incoming'
        data = self.payloads()[path]
        self.assertEqual(self.client.post(path, data=data).status_code, 403)
        sig = signature(origin + path, data)
        self.assertEqual(self.client.post(path, data=data, headers={'X-Twilio-Signature': sig}).status_code, 200)

    def test_json_is_rejected_instead_of_authenticating_only_empty_form(self):
        path = '/voice/dial-result'
        sig = signature(TEST_ORIGIN + path, {})
        response = self.client.post(path, json=self.payloads()[path], headers={'X-Twilio-Signature': sig})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.calls(), [])

    def test_wrong_token_cannot_authenticate(self):
        path = '/voice/incoming'
        data = self.payloads()[path]
        sig = signature(TEST_ORIGIN + path, data, 'wrong-dummy-token')
        self.assertEqual(self.client.post(path, data=data, headers={'X-Twilio-Signature': sig}).status_code, 403)

    def test_generic_event_cannot_bypass_twilio_protection(self):
        data = {'phone_number': '+15555550101', 'external_call_id': 'generic', 'call_status': 'no-answer'}
        for headers in ({}, {'Authorization': 'Bearer wrong'}, {'X-Twilio-Signature': signature(TEST_ORIGIN + '/call-event', data)}):
            self.assertEqual(self.client.post('/call-event', json=data, headers=headers).status_code, 403)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.client.post('/call-event', json=data, headers={'Authorization': 'Bearer ' + TEST_EVENT_TOKEN}).status_code, 201)
        self.app.config['CALL_EVENT_TOKEN'] = ''
        self.assertEqual(self.client.post('/call-event', json=data).status_code, 503)

    def test_public_pages_stay_available_and_error_responses_do_not_leak_secrets(self):
        self.app.config['TWILIO_AUTH_TOKEN'] = ''
        for path in ('/', '/privacy', '/terms'):
            self.assertEqual(self.client.get(path).status_code, 200)
        response = self.client.post('/voice/incoming', data={})
        for private in (TEST_TOKEN, TEST_EVENT_TOKEN, self.app.config['BUSINESS_PHONE']):
            self.assertNotIn(private.encode(), response.data)
