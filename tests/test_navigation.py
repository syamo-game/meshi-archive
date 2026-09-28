import unittest

from starlette.requests import Request

from web.routers.admin import templates


def _render_navigation(session: dict[str, object]) -> str:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "session": session,
        }
    )
    template = templates.env.get_template("base.html")
    return template.render(request=request, csrf_token=None)


class NavigationTest(unittest.TestCase):
    def test_admin_sees_data_confirmation_navigation(self) -> None:
        rendered = _render_navigation({"admin_authenticated": True})

        self.assertIn('href="/admin/review"', rendered)
        self.assertIn("データ確認", rendered)

    def test_regular_user_does_not_see_data_confirmation_navigation(self) -> None:
        rendered = _render_navigation({"authenticated": True})

        self.assertNotIn('href="/admin/review"', rendered)
        self.assertNotIn("データ確認", rendered)


if __name__ == "__main__":
    unittest.main()
