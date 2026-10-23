"""In-memory cookie sessions and bounded, non-redirecting stdlib HTTP."""

from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPException, HTTPResponse
from http.cookiejar import CookieJar
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .models import JSONObject, JSONValue


class ApiError(RuntimeError):
    """Sanitized request failure; HTTP status is available without response text."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _Unreachable(ApiError):
    """The connection failed, so the server may or may not have processed the request."""

    def __init__(self) -> None:
        super().__init__("Request failed; connection details withheld")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, new_url):
        return None


def _origin(value: str) -> str:
    message = "Use an HTTPS origin (HTTP only for localhost), without credentials, path, query or fragment"
    if not isinstance(value, str) or any(ord(character) <= 32 or ord(character) >= 127 for character in value):
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError(message) from None
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or "?" in value or "#" in value or "\\" in value
            or (port is not None and port == 0)
            or parsed.scheme not in ("http", "https")
            or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
        raise ValueError(message)
    return value.rstrip("/")


def _object(pairs: list[tuple[str, JSONValue]]) -> JSONObject:
    result: JSONObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


class Transport:
    """Synchronous client. Sessions stay in memory; instances are not thread-safe."""

    def __init__(self, origin: str, *, timeout: float = 30,
                 max_json_bytes: int = 16 * 1024 * 1024,
                 max_media_bytes: int = 4 * 1024 * 1024 * 1024) -> None:
        self.origin = _origin(origin)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        for limit in (max_json_bytes, max_media_bytes):
            if type(limit) is not int or limit <= 0:
                raise ValueError("Response size limits must be positive integers")
        self.timeout = timeout
        self.max_json_bytes = max_json_bytes
        self.max_media_bytes = max_media_bytes
        self._cookies = CookieJar()
        self._csrf: str | None = None
        self._opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self._cookies), _NoRedirect())

    def close(self) -> None:
        """Forget local credentials."""
        self._cookies.clear()
        self._csrf = None

    @contextmanager
    def _request(self, method: str, path: str, body: JSONObject | None = None,
                 *, media: bool = False) -> Iterator[HTTPResponse]:
        headers = {"Accept": "video/mp4" if media else "application/json", "Accept-Encoding": "identity"}
        if method != "GET" and path != "/api/auth/login":
            if self._csrf is None:
                raise ApiError("Sign in before making a modifying request")
            headers["X-CSRF-Token"] = self._csrf
        encoded = None
        if body is not None:
            encoded = json.dumps(body, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path, data=encoded, headers=headers, method=method)
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            status = error.code
            detail = None
            try:
                if status in (400, 404, 409, 503) and not path.startswith("/api/auth/"):
                    raw = error.read(4097)
                    if len(raw) <= 4096:
                        value = json.loads(raw)
                        value = value.get("detail") if isinstance(value, dict) else None
                        if isinstance(value, dict):
                            value = value.get("message")
                        if isinstance(value, str):
                            printable = "".join(character if character.isprintable() else " " for character in value)
                            detail = " ".join(printable.split())[:512]
            except (ValueError, OSError, HTTPException):
                pass
            finally:
                error.close()
            if status == 401:
                self.close()
                detail = "you are signed out; connect again"
            message = f"Server returned HTTP {status}"
            raise ApiError(message + (f": {detail}" if detail else "; response body withheld"), status=status) from None
        except (OSError, URLError, HTTPException):
            raise _Unreachable() from None
        # Errors raised by the caller's block, such as a full disk, are not connection failures.
        with response:
            if response.status not in (200, 201):
                raise ApiError("Unexpected HTTP response", status=response.status)
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ApiError("Encoded responses are not supported")
            yield response

    @staticmethod
    def _read(response: HTTPResponse, size: int) -> bytes:
        try:
            return response.read(size)
        except (OSError, HTTPException):
            raise _Unreachable() from None

    def _json(self, method: str, path: str, body: JSONObject | None = None) -> JSONObject:
        with self._request(method, path, body) as response:
            raw = self._read(response, self.max_json_bytes + 1)
        if len(raw) > self.max_json_bytes:
            raise ApiError("JSON exceeds configured size limit")
        try:
            value = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_constant,
                               parse_float=_finite_float)
            if not isinstance(value, dict):
                raise ValueError("Expected object")
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError("Invalid JSON object response") from None

    def _session(self, value: JSONObject) -> JSONObject:
        csrf = value.get("csrf_token")
        if not isinstance(csrf, str) or not re.fullmatch(r"[!-~]{1,1024}", csrf):
            self.close()
            raise ApiError("Session response has no valid CSRF token")
        self._csrf = csrf
        return value

    def login(self, username: str, password: str) -> JSONObject:
        self.close()
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise ValueError("username and password must be nonempty strings")
        try:
            return self._session(self._json("POST", "/api/auth/login", {"username": username, "password": password}))
        except ApiError as error:
            self.close()
            if error.status == 401:
                raise ApiError("Sign-in failed: check your username and password", status=401) from None
            if error.status == 429:
                raise ApiError("Sign-in failed: too many attempts; wait and try again later", status=429) from None
            raise
        except BaseException:
            self.close()
            raise
