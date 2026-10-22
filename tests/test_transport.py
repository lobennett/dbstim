import unittest
from unittest.mock import patch
from urllib.error import URLError
from dbstim._transport import ApiError, Transport
from tests.http_fixture import server
from tests.test_dstim import response


class TransportTests(unittest.TestCase):
    def test_actionable_details_only_for_expected_non_auth_errors(self):
        for status in (400, 409):
            with server(lambda *args: response({"detail": "Insufficient eligible videos"}, status)) as (origin, requests):
                with self.assertRaisesRegex(ApiError, "Insufficient eligible videos"):
                    Transport(origin)._json('GET', '/api/v1/metrics')
                with self.assertRaises(ApiError) as caught:
                    Transport(origin).login("user", "secret")
                self.assertNotIn("Insufficient", str(caught.exception))

    def test_reject_unsafe_origins_and_timeouts(self):
        for origin in ("http://example.org", "https://user:secret@example.org", "https://example.org/x",
                       "https://example.org?x=1", "https://example.org#x", "file:///tmp/x",
                       "https://example.org\n", "http://localhost.evil"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                Transport(origin)
        for timeout in (0, -1, True, float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                Transport("https://example.org", timeout=timeout)

    def test_redirect_never_sends_password_or_cookie_to_destination(self):
        with server(lambda *args: response({})) as (other, leaked):
            for status in (301, 302, 303, 307, 308):
                with server(lambda *args: (status, {"Location": other + "/steal"}, b"secret")) as (origin, requests):
                    with self.assertRaises(ApiError) as caught:
                        Transport(origin).login("user", "secret")
                    self.assertEqual(caught.exception.status, status)
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertEqual(len(requests), 1)
            self.assertEqual(leaked, [])

    def test_errors_invalid_json_and_auth_failure_clear_credentials(self):
        for payload, status in ((b"private password", 401), (b"not json", 200), (b"[]", 200),
                                (b'{"value":NaN}', 200), (b'{"value":1e999}', 200),
                                (b'{"x":1,"x":2}', 200)):
            with self.subTest(payload=payload):
                with server(lambda *args: (status, {}, payload)) as (origin, requests):
                    with self.assertRaises(ApiError) as caught:
                        Transport(origin)._json('GET', '/api/v1/metrics')
                    self.assertNotIn("private password", str(caught.exception))

    def test_timeout_passed_and_transport_details_withheld(self):
        client = Transport("https://example.org", timeout=0.25)
        with patch.object(client._opener, "open", side_effect=URLError("secret")) as opened:
            with self.assertRaises(ApiError) as caught:
                client._json('GET', '/api/v1/metrics')
            self.assertEqual(opened.call_args.kwargs["timeout"], 0.25)
            self.assertNotIn("secret", str(caught.exception))

    def test_json_size_bound(self):
        with server(lambda *args: response({"large": "x" * 100})) as (origin, requests):
            with self.assertRaisesRegex(ApiError, "size limit"):
                Transport(origin, max_json_bytes=50)._json('GET', '/api/v1/metrics')

    def test_authenticated_redirect_does_not_leak_cookie(self):
        with server(lambda *args: response({})) as (other, leaked):
            def respond(method, path, headers, body):
                if path.endswith("/login"):
                    return response({"csrf_token": "csrf"}, **{"Set-Cookie": "session=private; Path=/"})
                return 307, {"Location": other + "/steal"}, b""
            with server(respond) as (origin, requests):
                client = Transport(origin)
                client.login("user", "secret")
                with self.assertRaises(ApiError):
                    client._json('POST', '/api/v1/media/query', {})
                self.assertEqual(len(requests), 2)
                self.assertEqual(leaked, [])

    def test_expired_session_clears_cookie(self):
        def respond(method, path, headers, body):
            if path.endswith("/login"):
                return response({"csrf_token": "csrf"}, **{"Set-Cookie": "session=private; Path=/"})
            if path == "/api/v1/experiments":
                return response({}, 401)
            self.assertIsNone(headers.get("Cookie"))
            return response({})
        with server(respond) as (origin, requests):
            client = Transport(origin)
            client.login("user", "secret")
            with self.assertRaises(ApiError):
                client._json('GET', '/api/v1/experiments')
            client._json('GET', '/api/v1/metrics')
            with self.assertRaises(ApiError):
                client._json('POST', '/api/v1/media/query', {})
