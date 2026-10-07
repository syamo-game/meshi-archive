from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request

from db.models import Shop
from web.routers import home


class EditHtml(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: dict[str, dict[str, str | None]] = {}
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        element_id = attributes.get("id")
        if element_id:
            assert element_id not in self.ids
            self.ids[element_id] = attributes


def render_editor(
    *, admin: bool = True, editing: bool = True, image_url: str | None = None,
    errors: home.ShopEditErrors | None = None, failure: str | None = None,
) -> tuple[str, EditHtml]:
    request = Request({
        "type": "http", "method": "GET", "path": "/shop/12", "query_string": b"",
        "headers": [], "scheme": "http", "server": ("testserver", 80),
        "session": {"authenticated": True, "admin_authenticated": admin},
    })
    shop = Shop(id=12, shop_name="写真確認店", branch_name="銀座店", area="銀座", category="カフェ", is_visited=False)
    links: home.ShopLinks = {
        "maps_url": "https://www.google.com/maps?q=photo", "source_url": None,
        "source_is_map": False, "source_label": None, "canonical_url": None,
        "discord_url": None, "image_url": image_url,
    }
    values = home.ShopEditValues(
        shop_name="入力した店名", area="", category="", url="", address="", phone="",
        memo="保存前のメモ", rating="", is_visited=False, visited_at="", branch_name="入力した支店名",
    )
    html = home.templates.env.get_template("shop.html").render(
        request=request, shop=shop, shop_links={12: links}, is_admin=admin, is_editing=admin and editing, return_to="/?q=cafe",
        csrf_token="test-csrf", saved=False, edit_values=values, edit_errors=errors or {},
        edit_failure=failure, current_area_outside_master=False, grouped_areas=[], all_categories=[],
    )
    return html, EditHtml(html)


def test_admin_editor_submits_photo_with_existing_fields_and_previews_current_image() -> None:
    current = "/static/shop-images/uploaded-current.jpg"
    html, document = render_editor(image_url=current)
    form = document.ids["shop-edit-form"]
    assert form["method"] == "post"
    assert form["action"] == "/shop/12/edit"
    assert form["enctype"] == "multipart/form-data"
    photo = document.ids["shop-photo"]
    assert photo["name"] == "photo" and photo["type"] == "file"
    assert set((photo["accept"] or "").split(",")) == {"image/jpeg", "image/png", "image/webp"}
    assert photo["aria-describedby"] == "shop-photo-help"
    preview = document.ids["shop-photo-preview"]
    assert preview["src"] == preview["data-current-src"] == current
    assert "object-fit-cover" in (preview["class"] or "").split()
    assert "ratio-16x9" in html
    assert "hidden" not in preview
    assert "hidden" in document.ids["shop-photo-empty"]
    assert [attrs for tag, attrs in document.elements if tag == "img"] == [preview]
    assert "写真確認店" in html and "銀座店" in html
    assert "data-shop-detail-content" not in html
    assert document.ids["shop-edit-submit"]["type"] == "submit"
    assert '/static/js/area-picker.js?v=1' in html
    assert '/static/js/shop-edit.js?v=4' in html


def test_editor_renders_photo_error_and_retains_other_inputs_without_current_image() -> None:
    html, document = render_editor(errors={"photo": "JPEG・PNG・WebPの写真を選んでください。"})
    assert document.ids["shop-photo"]["aria-invalid"] == "true"
    assert document.ids["shop-photo"]["aria-describedby"] == "shop-photo-help shop-photo-error"
    assert "JPEG・PNG・WebPの写真を選んでください。" in html
    assert "hidden" in document.ids["shop-photo-preview"]
    assert "src" not in document.ids["shop-photo-preview"]
    assert "hidden" not in document.ids["shop-photo-empty"]
    assert document.ids["shop-name"]["value"] == "入力した店名"
    assert document.ids["shop-branch-name"]["value"] == "入力した支店名"
    assert document.ids["shop-branch-name"]["maxlength"] == "255"
    assert "保存前のメモ" in html
    assert "hidden" not in document.ids["shop-edit-error"]


def test_editor_renders_general_save_failure_without_requiring_field_errors() -> None:
    html, document = render_editor(failure="保存できませんでした。もう一度お試しください。")
    assert "保存できませんでした。もう一度お試しください。" in html
    assert document.ids["shop-edit-error"]["role"] == "alert"
    assert document.ids["shop-edit-error"]["tabindex"] == "-1"
    assert "hidden" not in document.ids["shop-edit-error"]


def test_regular_viewer_does_not_receive_photo_editor_or_its_script() -> None:
    html, document = render_editor(admin=False)
    assert "shop-edit-form" not in document.ids
    assert "shop-photo" not in document.ids
    assert "/static/js/shop-edit.js" not in html


def test_standard_detail_keeps_photo_and_links_to_the_separate_editor() -> None:
    html, document = render_editor(editing=False, image_url="/static/shop-images/current.jpg")
    assert "data-shop-detail-content" in html
    assert 'class="place-detail__photo m-0"' in html
    links = [attrs for tag, attrs in document.elements if tag == "a" and "data-full-detail-link" in attrs]
    assert len(links) == 1
    destination = urlsplit(links[0]["href"] or "")
    assert destination.path == "/shop/12"
    assert parse_qs(destination.query) == {"edit": ["true"], "return_to": ["/?q=cafe"]}
    assert destination.fragment == "shop-edit"
