from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from db.models import Base, Message, Shop, ShopMention, SourceAsset
from services.shop_image_upload import uploaded_image_public_url
from web.routers import home


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def detail_request(shop_id: int, *, admin: bool = False) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": f"/shop/{shop_id}",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("testserver", 80),
        "session": {"authenticated": True, "admin_authenticated": admin, "csrf_token": "test"},
    })


def add_shop(db: Session, branch_name: str, index: int) -> Shop:
    shop = Shop(shop_name="同名確認カフェ", branch_name=branch_name, area="銀座")
    db.add(ShopMention(
        shop=shop, message=Message(message_id=f"900000000000001{index:02d}"),
        occurrence_index=0, extracted_name=shop.shop_name,
        review_status="approved", metadata_review_status="approved",
    ))
    db.flush()
    return shop


def test_detail_shows_the_card_photo_and_an_accessible_enlargement_link(db: Session) -> None:
    shop = add_shop(db, "銀座店", 1)
    shop.image_key = "a" * 64
    source = "https://pbs.twimg.com/media/detail-photo.jpg"
    shop.mentions[0].message.assets.append(SourceAsset(kind="image", url=source, fetch_status="available"))
    db.flush()
    response = home.shop_detail(shop.id, detail_request(shop.id), db=db)
    html = response.body.decode()
    tree = HtmlTree()
    tree.feed(html)
    image = next(node for node in tree.nodes if node.tag == "img")
    enlargement = next(node for node in tree.nodes if (node.attrs.get("class") or "").startswith("place-detail__photo-link"))
    assert image.attrs["src"] == uploaded_image_public_url(shop.image_key)
    assert image.attrs["width"] == "960"
    assert image.attrs["height"] == "540"
    assert "銀座店" in (image.attrs["alt"] or "")
    assert enlargement.attrs["href"] == image.attrs["src"]
    assert enlargement.attrs["target"] == "_blank"
    assert enlargement.attrs["rel"] == "noopener noreferrer"
    assert "写真を拡大（新しいタブで開きます）" in (enlargement.attrs["aria-label"] or "")


@pytest.mark.parametrize(("admin", "editing", "branch"), [
    (True, True, "銀座店"), (True, True, "新宿店"),
    (True, False, "銀座店"), (False, True, "新宿店"),
])
def test_edit_mode_keeps_branch_and_uses_only_the_form_photo_preview(
    db: Session, admin: bool, editing: bool, branch: str,
) -> None:
    shop = add_shop(db, branch, 1)
    shop.image_key = "b" * 64
    shop.mentions[0].message.assets.append(SourceAsset(
        kind="image", url="https://pbs.twimg.com/media/editor-photo.jpg", fetch_status="available",
    ))
    db.flush()
    response = home.shop_detail(shop.id, detail_request(shop.id, admin=admin), db=db, edit=editing)
    html = response.body.decode()
    tree = HtmlTree()
    tree.feed(html)
    images = [node for node in tree.nodes if node.tag == "img"]
    assert branch in html
    assert "同名確認カフェ" in html
    if admin and editing:
        assert len(images) == 1
        assert images[0].attrs.get("id") == "shop-photo-preview"
        assert "data-shop-detail-content" not in html
        assert 'class="shop-edit-header mb-4"' in html
    else:
        assert "data-shop-detail-content" in html
        assert any("place-detail__image" in (image.attrs.get("class") or "") for image in images)
        assert 'class="shop-edit-header mb-4"' not in html


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[HtmlNode | str] = field(default_factory=list)


class HtmlTree(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = HtmlNode("root")
        self.stack: list[HtmlNode] = [self.root]
        self.nodes: list[HtmlNode] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = HtmlNode(tag, dict(attrs))
        self.stack[-1].children.append(node)
        self.nodes.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


@pytest.mark.parametrize("branch", ["銀座店", "新宿店"])
def test_dialog_title_move_keeps_each_branch_name(db: Session, branch: str) -> None:
    node_executable = shutil.which("node")
    if node_executable is None:
        pytest.skip("Node.js is required to execute the dialog title transformation")
    shops = [add_shop(db, name, index) for index, name in enumerate(["銀座店", "新宿店"], 1)]
    shop = next(item for item in shops if item.branch_name == branch)
    response = home.shop_detail(shop.id, detail_request(shop.id), db=db)
    tree = HtmlTree()
    tree.feed(response.body.decode())
    script = Path("web/static/js/main.js").read_text(encoding="utf-8")
    start = script.index("var title = content.querySelector('[data-detail-title]');")
    end = script.index("detailBody.replaceChildren(content);", start)
    payload = json.dumps({"tree": asdict(tree.root), "transform": script[start:end], "branch": branch})
    harness = r"""
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor(value, parent = null) {
    this.attrs = value.attrs;
    this.parent = parent;
    this.children = value.children.map(child => typeof child === 'string' ? child : new Element(child, this));
  }
  get textContent() { return this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
  get childElementCount() { return this.children.filter(child => typeof child !== 'string').length; }
  matches(selector) { return Object.hasOwn(this.attrs, selector.slice(1, -1)); }
  querySelector(selector) {
    for (const child of this.children) {
      if (typeof child === 'string') continue;
      if (child.matches(selector)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
  closest(selector) {
    for (let current = this; current; current = current.parent) if (current.matches(selector)) return current;
    return null;
  }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
}
const content = new Element(input.tree).querySelector('[data-shop-detail-content]');
const detailTitle = { textContent: '' };
vm.runInNewContext(input.transform, { content, detailTitle });
if (!content.textContent.includes(input.branch)) throw new Error('Branch disappeared from the dialog');
if (detailTitle.textContent !== '同名確認カフェ') throw new Error('Dialog title was not transferred');
if (content.querySelector('[data-detail-title]')) throw new Error('Duplicate title remained in the body');
"""
    subprocess.run([node_executable, "-e", harness], input=payload, text=True, encoding="utf-8", check=True, capture_output=True)
