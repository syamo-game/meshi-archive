from dataclasses import dataclass
from html.parser import HTMLParser

from starlette.requests import Request

from web.routers.review import review_page


@dataclass(frozen=True)
class ReviewElement:
    tag: str
    attrs: dict[str, str | None]
    ancestors: tuple["ReviewElement", ...]


class ReviewHtml(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[ReviewElement] = []
        self.stack: list[ReviewElement] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = ReviewElement(tag, dict(attrs), tuple(self.stack))
        self.elements.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and self.stack[-1].tag == tag:
            self.stack.pop()

    def by_id(self, element_id: str) -> ReviewElement:
        matches = [element for element in self.elements if element.attrs.get("id") == element_id]
        assert len(matches) == 1, f"Expected one element with id={element_id}"
        return matches[0]


def render_review() -> tuple[str, ReviewHtml]:
    request = Request({
        "type": "http", "method": "GET", "path": "/admin/review", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("testserver", 80),
        "session": {"authenticated": True, "admin_authenticated": True, "csrf_token": "test-token"},
    })
    response = review_page(request)
    assert response.status_code == 200
    html = bytes(response.body).decode("utf-8")
    return html, ReviewHtml(html)


def test_review_filter_keeps_scope_in_form_without_exposing_internal_codes() -> None:
    html, document = render_review()
    form = document.by_id("review-filter")
    scope = document.by_id("review-scope")
    assert form in scope.ancestors
    assert scope.attrs["type"] == "hidden"
    assert scope.attrs["value"] == "identity"
    names = {
        element.attrs.get("name") for element in document.elements
        if form in element.ancestors and element.tag in {"input", "select"}
    }
    assert names == {"scope", "status", "q"}
    for scope_name, pressed in (("identity", "true"), ("metadata", "false")):
        button = document.by_id(f"review-scope-{scope_name}")
        assert form in button.ancestors
        assert document.by_id("review-scope-switch") in button.ancestors
        assert button.attrs["type"] == "button"
        assert button.attrs["aria-pressed"] == pressed
    assert "管理へ戻る" not in html
    assert document.by_id("review-result-count")
    assert document.by_id("review-result-context")
    assert "1つの投稿に2店あれば2件" in html


def test_review_separates_processing_details_and_keeps_error_recovery_visible() -> None:
    _, document = render_review()
    for element_id in (
        "count-unavailable", "count-failed", "review-message-id", "review-difference-code",
        "review-extraction-source", "review-confidence", "review-group-reason", "review-extraction-error",
    ):
        element = document.by_id(element_id)
        assert any(parent.tag == "details" and "open" not in parent.attrs for parent in element.ancestors)
    reason = document.by_id("review-difference")
    assert not any(parent.tag == "details" for parent in reason.ancestors)
    warning = document.by_id("review-source-warning")
    assert document.by_id("review-evidence") in warning.ancestors
    assert warning.attrs["role"] == "status"
    error = document.by_id("review-error")
    assert error.attrs["tabindex"] == "-1"
    assert not any("hidden" in parent.attrs or parent.tag == "details" for parent in error.ancestors)
    assert error in document.by_id("review-retry").ancestors
    assert document.by_id("review-edit-form") in document.by_id("review-edit-scope-hint").ancestors
