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
    assert scope.attrs["value"] == "all"
    names = {
        element.attrs.get("name") for element in document.elements
        if form in element.ancestors and element.tag in {"input", "select"}
    }
    assert names == {"scope", "status", "q"}
    for scope_name, pressed in (("all", "true"), ("identity", "false"), ("metadata", "false")):
        button = document.by_id(f"review-scope-{scope_name}")
        assert form in button.ancestors
        assert document.by_id("review-scope-switch") in button.ancestors
        assert button.attrs["type"] == "button"
        assert button.attrs["aria-pressed"] == pressed
    assert "管理へ戻る" not in html
    assert document.by_id("review-result-count")
    assert document.by_id("review-result-context")
    assert "登録内容の確認" in html
    assert document.by_id("count-unresolved")
    assert not any(element.attrs.get("id") in {"count-failed", "count-unavailable"} for element in document.elements)


def test_review_hides_legacy_tools_and_keeps_error_recovery_visible() -> None:
    html, document = render_review()
    legacy = document.by_id("review-legacy-tools")
    assert "hidden" in legacy.attrs and "inert" in legacy.attrs
    assert "その他の操作" not in html
    for element_id in (
        "review-message-id", "review-difference-code",
        "review-extraction-source", "review-confidence", "review-group-reason", "review-extraction-error",
    ):
        element = document.by_id(element_id)
        assert legacy in element.ancestors
    reason = document.by_id("review-difference")
    assert legacy in reason.ancestors
    warning = document.by_id("review-source-warning")
    assert document.by_id("review-decision") in warning.ancestors
    assert not any(parent.tag == "details" for parent in warning.ancestors)
    assert warning.attrs["role"] == "status"
    error = document.by_id("review-error")
    assert error.attrs["tabindex"] == "-1"
    assert not any("hidden" in parent.attrs or parent.tag == "details" for parent in error.ancestors)
    assert error in document.by_id("review-retry").ancestors
    assert legacy in document.by_id("review-edit-scope-hint").ancestors
    for element_id in ("review-link-form", "review-merge-form", "review-candidates", "review-assets"):
        assert legacy in document.by_id(element_id).ancestors


def test_single_mention_link_is_separate_from_merge_and_requires_evidence_and_confirmation() -> None:
    html, document = render_review()
    form = document.by_id("review-link-form")
    merge = document.by_id("review-merge-form")
    assert merge not in form.ancestors and form not in merge.ancestors
    assert document.by_id("review-link-section") in form.ancestors
    target = document.by_id("review-link-target")
    note = document.by_id("review-link-note")
    assert form in target.ancestors and form in note.ancestors
    assert target.attrs["type"] == "number" and target.attrs["min"] == "1"
    assert "required" in target.attrs and "required" in note.attrs
    assert note.attrs["maxlength"] == "2000"
    assert document.by_id("review-link-preview-submit").attrs["type"] == "submit"
    confirmation = document.by_id("review-link-confirm")
    assert confirmation.attrs["type"] == "checkbox" and "checked" not in confirmation.attrs
    apply = document.by_id("review-link-apply")
    assert apply.attrs["type"] == "button" and "disabled" in apply.attrs
    assert document.by_id("review-link-preview") in apply.ancestors
    assert "hidden" in document.by_id("review-link-preview").attrs
    assert "この投稿だけを既存店舗に関連付ける" in html
    assert "選択中の確認項目1件だけ" in html
    assert "訪問状態・訪問日・評価・メモ" in html
    assert "エリア・カテゴリの確認状態は変更しません" in html
    assert "元投稿・添付・確認履歴も残ります" in html
    assert "店舗自体は削除しません" in html
    assert "関連件数・代表投稿・公開一覧" in html
    assert "/static/js/review.js?v=23" in html


def test_registration_has_two_actions_and_inline_supplement_and_photo() -> None:
    html, document = render_review()
    note = document.by_id("review-supplement")
    assert note.tag == "textarea" and note.attrs["maxlength"] == "4000"
    assert "required" not in note.attrs
    form = document.by_id("review-edit-form")
    assert form in note.ancestors
    submit = document.by_id("review-edit-submit")
    assert submit.attrs["type"] == "submit" and submit.attrs["form"] == "review-edit-form"
    assert document.by_id("review-reject").attrs["type"] == "button"
    assert not any(element.attrs.get("id") in {"review-defer", "review-approve"} for element in document.elements)
    ai = document.by_id("review-ai-search")
    assert ai.attrs["type"] == "button" and "disabled" in ai.attrs
    assert form not in ai.ancestors
    assert "AIで再読み込み" in html
    for direction in ("previous", "next"):
        arrow = document.by_id(f"review-photo-{direction}")
        assert arrow.attrs["type"] == "button" and arrow.attrs["aria-label"]
    assert document.by_id("review-photo").attrs["alt"]
    selection = document.by_id("review-photo-select")
    assert selection.attrs["type"] == "checkbox"
    assert form in selection.ancestors
    assert document.by_id("review-photo-selection") in selection.ancestors
    assert document.by_id("review-photo-selection-status").attrs["role"] == "status"
    assert document.by_id("review-notice").attrs["role"] == "status"
