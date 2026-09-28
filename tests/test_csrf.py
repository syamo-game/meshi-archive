import unittest
from typing import Optional

from fastapi import HTTPException
from starlette.requests import Request

from web.csrf import get_csrf_token, verify_csrf_token


def _request(
    session: Optional[dict[str, str]] = None,
    headers: Optional[dict[str, str]] = None,
) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "headers": raw_headers, "session": session or {}})


class CsrfTest(unittest.TestCase):
    def test_get_csrf_token_reuses_session_value(self) -> None:
        request = _request({"csrf_token": "known-token"})

        self.assertEqual(get_csrf_token(request), "known-token")

    def test_verify_accepts_form_token(self) -> None:
        request = _request({"csrf_token": "known-token"})

        verify_csrf_token(request, "known-token")

    def test_verify_accepts_header_token(self) -> None:
        request = _request(
            {"csrf_token": "known-token"},
            {"X-CSRF-Token": "known-token"},
        )

        verify_csrf_token(request)

    def test_verify_rejects_missing_or_mismatched_token(self) -> None:
        request = _request({"csrf_token": "known-token"})

        with self.assertRaises(HTTPException):
            verify_csrf_token(request, "wrong-token")


if __name__ == "__main__":
    unittest.main()
