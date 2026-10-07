import unittest

from starlette.requests import Request

from web.routers.admin import templates


def _render_navigation(session: dict[str, object], path: str = "/") -> str:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
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
    def test_admin_sees_management_and_logout_in_header_menu(self) -> None:
        for path in ("/", "/shop/1", "/admin", "/admin/review", "/admin/users", "/admin/shops/bulk", "/admin/exports", "/admin/import/validate"):
            with self.subTest(path=path):
                rendered = _render_navigation({"admin_authenticated": True}, path)
                header = rendered.split("</header>", 1)[0]
                account_menu = header.split('id="account-menu-links"', 1)[1].split("</nav>", 1)[0]
                self.assertIn('aria-label="メニュー"', header)
                self.assertIn('href="/" class="site-header__logo', header)
                self.assertIn('href="/admin"', account_menu)
                self.assertIn('href="/logout"', account_menu)
                self.assertEqual(account_menu.count("<a "), 2)
                self.assertNotIn('href="/admin/review"', header)
                self.assertEqual(rendered.count('role="banner"'), 1)
                if path.startswith("/admin"):
                    menu = rendered.split('class="admin-navigation"', 1)[1].split("</nav>", 1)[0]
                    self.assertEqual(menu.count("<a "), 5)
                    for destination in ("/admin", "/admin/review", "/admin/users", "/admin/shops/bulk", "/admin/exports"):
                        self.assertIn(f'href="{destination}"', menu)
                    self.assertEqual(menu.count('aria-current="page"'), 1)
                    self.assertLess(rendered.index("</header>"), rendered.index('class="admin-sidebar"'))
                    self.assertNotIn('class="admin-brand"', rendered)
                    self.assertNotIn('class="admin-logout"', rendered)
                    self.assertEqual(rendered.count("ログアウト"), 1)
                    self.assertNotIn("サイトを見る", rendered)
                self.assertNotIn("<footer", rendered)

    def test_regular_user_sees_only_logout_in_header_menu(self) -> None:
        for path in ("/", "/shop/1", "/admin/login"):
            with self.subTest(path=path):
                rendered = _render_navigation({"authenticated": True}, path)
                menu = rendered.split('id="account-menu-links"', 1)[1].split("</nav>", 1)[0]
                self.assertIn('href="/logout"', menu)
                self.assertEqual(menu.count("<a "), 1)
                self.assertNotIn('href="/admin"', rendered)
                self.assertNotIn('href="/admin/review"', rendered)
                self.assertNotIn("<footer", rendered)

    def test_anonymous_user_sees_only_login_in_header_menu(self) -> None:
        for path in ("/", "/login", "/admin/login"):
            with self.subTest(path=path):
                rendered = _render_navigation({}, path)
                menu = rendered.split('id="account-menu-links"', 1)[1].split("</nav>", 1)[0]
                self.assertIn('href="/login"', menu)
                self.assertEqual(menu.count("<a "), 1)
                self.assertNotIn('href="/admin"', rendered)
                self.assertNotIn('href="/logout"', rendered)


if __name__ == "__main__":
    unittest.main()
