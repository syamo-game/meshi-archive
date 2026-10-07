import unittest

from starlette.requests import Request

from web.routers.admin import templates
from web.routers.home import perform_logout


def _request(session: dict[str, object]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/login",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "session": session,
        }
    )


def _render_admin_login(session: dict[str, object]) -> str:
    template = templates.env.get_template("admin_login.html")
    return template.render(
        request=_request(session),
        error=None,
        csrf_token="test-token",
    )


class AdminLoginTest(unittest.TestCase):
    def test_authenticated_user_sees_logout_link(self) -> None:
        rendered = _render_admin_login({"authenticated": True})

        self.assertIn('href="/logout"', rendered)
        self.assertIn("現在のユーザーからログアウト", rendered)

    def test_unauthenticated_user_does_not_see_logout_link(self) -> None:
        rendered = _render_admin_login({})

        self.assertNotIn('href="/logout"', rendered)

    def test_logout_clears_user_and_admin_authentication(self) -> None:
        session: dict[str, object] = {
            "authenticated": True,
            "admin_authenticated": True,
            "discord_user_id": "123456789",
            "discord_username": "test-user",
            "csrf_token": "keep-token",
        }
        response = perform_logout(_request(session), csrf_token="keep-token")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(session, {"csrf_token": "keep-token"})


if __name__ == "__main__":
    unittest.main()
