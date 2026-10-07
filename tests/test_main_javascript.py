from pathlib import Path
import shutil
import subprocess

import pytest


MAIN_SCRIPT = Path("web/static/js/main.js")


@pytest.mark.parametrize("scenario", [
    "delete_cancel", "delete_accept", "import_empty", "import_valid",
    "apply_empty", "apply_cancel", "apply_accept",
])
def test_confirmation_and_csv_validation_open_only_the_needed_dialog(
    scenario: str, tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[2];
class Control {
  constructor() { this.listeners = {}; this.attributes = {}; this.hidden = true; this.files = []; this.focusCount = 0; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  querySelector() { return input; }
  focus() { this.focusCount += 1; }
  dispatch(type) {
    const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (const listener of this.listeners[type] || []) listener(event);
    return event;
  }
}
const form = new Control(), input = new Control(), error = new Control();
form.attributes['data-confirm-message'] = 'Delete synthetic shop and its saved memo; keep original posts?';
const kind = scenario.split('_')[0];
const elements = new Map(kind === 'import'
  ? [['import-form', form], ['import-file-error', error]]
  : kind === 'apply' ? [['import-apply-form', form], ['apply-file-error', error]] : []);
const document = {
  querySelector: () => null,
  querySelectorAll: selector => selector === 'form[data-confirm-message]' && kind === 'delete' ? [form] : [],
  getElementById: id => elements.get(id) || null, addEventListener() {},
};
const confirmations = [];
const pageEvents = new Map();
const window = {
  location: { hash: '', search: '' }, sessionStorage: { getItem: () => null, removeItem() {} },
  addEventListener(type, callback) { (pageEvents.get(type) || pageEvents.set(type, []).get(type)).push(callback); },
  requestAnimationFrame: callback => callback(),
  alert() { assert.fail('File errors and success must not open alerts'); },
  prompt() { assert.fail('No input popup is needed'); },
  confirm(message) { confirmations.push(message); return scenario.endsWith('_accept'); },
};
vm.runInNewContext(fs.readFileSync('web/static/js/main.js', 'utf8'), {
  document, window, console, URL, URLSearchParams, fetch() { assert.fail('Validation must not send an API request'); },
});
if (!scenario.endsWith('_empty')) input.files = [{ name: 'synthetic.csv' }];
const event = form.dispatch('submit');
if (kind === 'delete') {
  assert.equal(confirmations.length, 1, 'A delete submission needs exactly one confirmation');
  assert.equal(event.defaultPrevented, scenario.endsWith('_cancel'));
} else if (scenario.endsWith('_empty')) {
  assert.equal(event.defaultPrevented, true);
  assert.equal(confirmations.length, 0);
  assert.equal(error.hidden, false);
  assert.equal(input.attributes['aria-invalid'], 'true');
  assert.equal(input.focusCount, 1);
  input.files = [{ name: 'synthetic.csv' }]; input.dispatch('change');
  assert.equal(error.hidden, true);
  assert.equal(input.attributes['aria-invalid'], undefined);
} else if (kind === 'import') {
  assert.equal(event.defaultPrevented, false);
  assert.equal(confirmations.length, 0);
} else {
  assert.equal(confirmations.length, 1, 'CSV apply keeps one explicit confirmation');
  assert.match(confirmations[0], /空欄の項目は消去/);
  assert.equal(event.defaultPrevented, scenario.endsWith('_cancel'));
}
if (['delete_accept', 'apply_accept'].includes(scenario)) {
  const repeated = form.dispatch('submit');
  assert.equal(repeated.defaultPrevented, true, 'A second submit before navigation must be blocked');
  assert.equal(confirmations.length, 1, 'A repeated submit must not open another dialog');
  for (const callback of pageEvents.get('pageshow') || []) callback();
  const afterBack = form.dispatch('submit');
  assert.equal(afterBack.defaultPrevented, false, 'Browser back must allow an intentional new submit');
  assert.equal(confirmations.length, 2);
}
"""
    path = tmp_path / "popup-validation.cjs"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(path), scenario], capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", ["sequence", "conflict", "response_lost"])
def test_inline_actions_preserve_screen_versions_and_do_not_replay_conflicts(scenario: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[2];
class Element {
  constructor() { this.dataset = {}; this.disabled = false; this.children = []; this.textContent = ''; this.attributes = {}; this.classList = { toggle() {}, remove() {} }; }
  setAttribute(k, v) { this.attributes[k] = v; }
  getAttribute(k) { return this.attributes[k]; }
  removeAttribute(k) { delete this.attributes[k]; }
  appendChild(child) { this.children.push(child); }
  focus() {}
  remove() { elements.delete(this.id); }
}
const button = new Element();
button.dataset = {shopId: '1', shopVersion: '1', visited: 'false'};
const container = new Element();
container.dataset = {shopId: '1', shopVersion: '1', rating: '3'};
const stars = [1,2,3,4,5].map(value => { const s = new Element(); s.dataset.value = String(value); s.closest = q => q.includes('.star-rating') ? (q === '.star-rating' ? container : s) : null; return s; });
container.querySelectorAll = () => stars;
button.closest = q => q === '.btn-visit-toggle' ? button : null;
const elements = new Map();
const handlers = new Map();
const main = { prepend: box => elements.set(box.id, box) };
const document = {
  activeElement: null, body: {}, documentElement: {},
  querySelector: q => q.startsWith('.main-content') ? main : null,
  querySelectorAll: q => q === '[data-shop-version]' ? [button, container] : [],
  getElementById: id => elements.get(id) || null,
  createElement: () => new Element(), contains: () => true,
  addEventListener: (event, fn) => { if (!handlers.has(event)) handlers.set(event, []); handlers.get(event).push(fn); },
};
const window = { location: {hash:'',search:''}, sessionStorage: {getItem:()=>null,removeItem(){}}, addEventListener(){}, requestAnimationFrame: fn => fn() };
let version = 1, applied = 0, visited = false;
const calls = [];
async function fetchRequest(url, options) {
  calls.push({url,options});
  const expected = Number(options.headers['X-Shop-Version']);
  if (scenario === 'conflict' || expected !== version) return {ok:false,status:409,json:async()=>({detail:'Updated by another user'})};
  applied += 1; version += 1;
  if (url.endsWith('/visited')) visited = !visited;
  if (scenario === 'response_lost' && calls.length === 1) throw new Error('Response lost');
  return {ok:true,status:200,json:async()=>url.endsWith('/visited') ? {is_visited:visited,visited_at:'2026-10-04',version} : {rating:JSON.parse(options.body).rating,version}};
}
vm.runInNewContext(fs.readFileSync('web/static/js/main.js','utf8'),{document,window,fetch:fetchRequest,console,URLSearchParams,URL});
const flush = () => new Promise(resolve=>setImmediate(resolve));
async function click(target) { for (const fn of handlers.get('click')) fn({target,button:0}); await flush(); }
(async()=>{
  await click(button);
  assert.equal(calls[0].options.headers['X-Shop-Version'],'1');
  if (scenario === 'sequence') {
    assert.equal(button.dataset.shopVersion,'2');
    assert.equal(container.dataset.shopVersion,'2');
    await click(stars[4]);
    assert.equal(calls[1].options.headers['X-Shop-Version'],'2');
    assert.equal(button.dataset.shopVersion,'3');
    assert.equal(container.dataset.rating,5);
    assert.equal(applied,2);
    return;
  }
  assert.equal(button.dataset.shopVersion,'1');
  assert.equal(button.dataset.visited,'false');
  assert.equal(calls.length,1,'A failure must not automatically replay');
  if (scenario === 'response_lost') { await click(button); assert.equal(calls[1].options.headers['X-Shop-Version'],'1'); assert.equal(applied,1); assert.equal(visited,true); }
  else assert.equal(applied,0);
  assert.match(elements.get('global-action-error').textContent,/最新/);
  assert.equal(elements.get('global-action-error').children[0].target,'_blank');
  assert.equal(button.dataset.shopVersion,'1','A conflict must not silently adopt a new version');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    path = tmp_path / "inline-actions.cjs"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(path), scenario], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_list_uses_explicit_server_pagination() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "IntersectionObserver" not in script
    assert "filterForm.submit()" not in script
    assert "filterForm.addEventListener('submit'" in script


def test_detail_drawer_preserves_progressive_navigation_and_history() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "a[data-shop-detail]" in script
    assert "event.button !== 0" in script
    assert "event.metaKey" in script
    assert "event.ctrlKey" in script
    assert "event.shiftKey" in script
    assert "event.altKey" in script
    assert "window.history.pushState" in script
    assert "window.addEventListener('popstate'" in script
    assert "window.history.replaceState" in script
    assert "trigger.focus({ preventScroll: true })" in script


def test_detail_drawer_restores_saved_scroll_and_marks_current_row() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "historyState.listScrollY" in script
    assert "historyState.listTableScrollTop" in script
    assert "top: restoreScrollY" in script
    assert "tableWrapper.scrollTop = restoreTableScrollTop" in script
    assert "ensureRowVisible(trigger)" in script
    assert "setCurrentRow(activeRowId)" in script
    assert "row.setAttribute('aria-current', 'true')" in script
    assert "setCurrentRow(null)" in script


def test_detail_drawer_restores_row_from_url_or_falls_back_to_results() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "var rowId = params.get('row');" in script
    assert "openDetail(shopId, trigger, false, rowId);" in script
    assert "rowShopId: activeRowId" in script
    assert "detailStateUrl(activeShopId, activeRowId)" in script
    assert "trigger = detailTrigger(rowId)" in script
    assert "focusResultHeading('元の店舗は現在の検索結果にありません。')" in script
    assert "if (message && resultStatus) resultStatus.textContent = message;" in script


def test_removed_filter_moves_focus_to_query_input() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "var focusFilterFromHash = window.location.hash === '#q-input'" in script
    assert "filterFocusTarget.focus()" in script


def test_detail_drawer_aborts_stale_requests_and_offers_retry() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "new AbortController()" in script
    assert "activeRequest.abort()" in script
    assert "sequence !== requestSequence" in script
    assert "data-dialog-retry" in script
    assert "loadDetail(lastRequestUrl)" in script
    assert "role=\"alert\"" in script
    assert "detailBody.removeAttribute('aria-busy')" in script


def test_detail_drawer_separates_live_status_and_manages_focus_on_retry() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "document.getElementById('detail-dialog-status')" in script
    assert "detailBody.removeAttribute('aria-live')" in script
    assert "setDetailStatus('店舗情報を読み込んでいます。')" in script
    assert "setDetailStatus(detailTitle.textContent + 'の店舗情報を読み込みました。')" in script
    assert "if (detailDialog.open && document.contains(retryButton)) retryButton.focus();" in script
    assert "var closeButton = detailDialog.querySelector('[data-dialog-close]');" in script
    assert "if (closeButton) closeButton.focus();" in script


def test_detail_drawer_locks_root_while_open() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "document.documentElement.classList.add('is-dialog-open')" in script
    assert "document.documentElement.classList.remove('is-dialog-open')" in script


def test_visible_filters_keep_focus_without_resize_toggles() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")
    template = Path("web/templates/explore.html").read_text(encoding="utf-8")

    assert '<details class="explorer-filters"' not in template
    assert "syncAdvancedFilters" not in script
    assert "advancedFilters.removeAttribute('open')" not in script
    assert "advancedFilters.setAttribute('open', '')" not in script
    assert "window.location.hash === '#q-input'" in script
    assert "filterFocusTarget.focus()" in script
    assert "thead .sort-link" not in script


def test_result_navigation_restores_focus_after_full_page_load() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "window.sessionStorage.setItem(resultFocusStorageKey, target)" in script
    assert "window.sessionStorage.getItem(resultFocusStorageKey)" in script
    assert "saveResultFocusRequest('filter')" in script
    assert "requestedFocus !== 'filter' && !focusFilterFromHash" in script
    assert "window.sessionStorage.removeItem(resultFocusStorageKey)" in script
    assert "console.error('Result focus request could not be saved'" in script
    assert "console.error('Result focus request could not be restored'" in script
    assert "focusResultHeading('検索結果を更新しました。')" in script
    assert "[data-result-navigation], .pagination__control[href], .pagination__page[href]" in script
    assert "event.defaultPrevented" in script
    assert "event.button !== 0" in script
    assert "event.metaKey" in script
    assert "event.ctrlKey" in script
    assert "event.shiftKey" in script
    assert "event.altKey" in script


def test_filter_form_resets_browser_restored_values_to_server_state() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "window.addEventListener('pageshow'" in script
    assert "filterForm.reset()" in script
    assert "if (event.persisted)" in script
    assert "searchResults.removeAttribute('aria-busy')" in script


def test_inline_admin_actions_send_csrf_and_report_failures() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "headers: csrfHeaders({ 'X-Shop-Version': String(expectedVersion) })" in script
    assert "headers: csrfHeaders({ 'Content-Type': 'application/json', 'X-Shop-Version': String(expectedVersion) })" in script
    assert "Visit status request failed" in script
    assert "Rating request failed" in script
    assert "showActionError" in script
    assert "restoreButtonFocus" in script
    assert "restoreStarFocus" in script
    assert "buttonFocusWasLost" in script
    assert "starFocusWasLost" in script
    assert "box.focus()" in script


def test_inline_admin_actions_keep_facets_and_sorted_results_consistent() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "function updateStatusFacetCounts" not in script
    assert "changeStatusFacetCount" not in script
    assert "new URLSearchParams(window.location.search).has('status')" in script
    assert "activeSort === 'rating_asc' || activeSort === 'rating_desc'" in script
    assert "window.location.reload()" in script
    reload_sequence = (
        "saveResultFocusRequest();\n"
        "          setResultsLoading();\n"
        "          window.location.reload();"
    )
    assert script.count(reload_sequence) == 2


def test_json_parse_errors_fail_loudly() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "Invalid JSON response: status=" in script
    assert "response.json().catch(function (error)" in script
    assert "catch(function () { return {}; })" not in script
    assert "Invalid visit status response payload" in script
    assert "typeof data.is_visited !== 'boolean'" in script
    assert "Invalid rating response payload" in script
    assert "Number.isInteger(data.rating)" in script
    assert "clearActionError()" in script


def test_broken_shop_images_are_logged_and_replaced_with_the_plain_cover() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "function removeBrokenShopImage(image)" in script
    assert "Shop image could not be loaded" in script
    assert "[data-shop-image]" in script
    assert "imageLink.remove()" in script
    assert "if (media) media.remove()" not in script
    assert "media.classList.add('place-card__media--empty')" in script
    assert "media.setAttribute('aria-hidden', 'true')" in script
    assert "imageLink.contains(document.activeElement)" in script
    assert "titleLink.focus({ preventScroll: true })" in script
    assert "card.classList.remove('place-card--with-image')" in script
    assert "image.complete && image.naturalWidth === 0" in script


def test_row_restore_handles_page_scrolling_on_mobile() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "wrapperCanScroll" in script
    assert "window.getComputedStyle(tableWrapper)" in script
    assert "row.scrollIntoView({ behavior: 'auto', block: 'nearest'" in script
