from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVIEW_SCRIPT = PROJECT_ROOT / "web" / "static" / "js" / "review.js"


def test_review_queue_selects_the_next_remaining_item_after_group_resolution() -> None:
    script = REVIEW_SCRIPT.read_text(encoding="utf-8")

    helper = script.index("function nextRemainingItemId")
    next_scan = script.index("currentIndex + 1", helper)
    previous_scan = script.index("currentIndex - 1", next_scan)
    removed_ids = script.index("var removedIds = new Set")
    next_item = script.index("var nextItemId = nextRemainingItemId", removed_ids)
    queue_filter = script.index("state.items = state.items.filter", next_item)
    selected_lookup = script.index("state.items.findIndex", queue_filter)

    assert helper < next_scan < previous_scan
    assert removed_ids < next_item < queue_filter < selected_lookup
    assert "if (state.selectedIndex >= state.items.length)" not in script


def test_review_queue_uses_roving_focus_for_selected_option() -> None:
    script = REVIEW_SCRIPT.read_text(encoding="utf-8")

    assert "button.setAttribute('role', 'option');" in script
    assert "button.setAttribute('aria-selected'" in script
    assert "button.tabIndex = index === state.selectedIndex ? 0 : -1;" in script
    assert "function focusSelectedItem()" in script
    assert "selectedItem.focus({ preventScroll: true });" in script
    assert "selectedItem.scrollIntoView({ block: 'nearest', inline: 'nearest' });" in script


def test_review_shortcuts_are_scoped_and_blocked_while_busy() -> None:
    script = REVIEW_SCRIPT.read_text(encoding="utf-8")

    handler = script.index("document.addEventListener('keydown'")
    busy_guard = script.index("state.busy", handler)
    scope_guard = script.index("!isShortcutScopeActive(event.target)", busy_guard)
    target_guard = script.index("isShortcutBlockedTarget(event.target)", scope_guard)
    shortcut = script.index("var key = event.key.toLowerCase();", target_guard)

    assert handler < busy_guard < scope_guard < target_guard < shortcut
    assert "target === workspace || target === queue || queue.contains(target)" in script
    assert 'button:not([role="option"])' in script
    assert '[contenteditable="true"]' in script


def test_review_queue_supports_standard_listbox_navigation_keys() -> None:
    script = REVIEW_SCRIPT.read_text(encoding="utf-8")

    handler = script.index("document.addEventListener('keydown'")
    navigation = script[handler:]

    assert "event.key === 'ArrowDown'" in navigation
    assert "event.key === 'ArrowUp'" in navigation
    assert "event.key === 'Home'" in navigation
    assert "event.key === 'End'" in navigation
    assert "event.preventDefault();\n      selectItem(nextIndex);" in navigation
    assert "if (event.repeat) return;" in navigation


@pytest.mark.parametrize("scenario", [
    "server", "network", "conflict", "counters", "reload", "dirty", "busy", "filters", "failed_filters", "cannot_approve", "queue_focus",
    "scope_switch", "scope_failure", "candidate_draft", "metadata_branch", "reasons_and_counts", "last_item",
    "draft_reload_conflict", "draft_scope_conflict", "draft_removed_conflict", "reject",
])
def test_review_retry_preserves_edits_and_repeats_only_the_failed_request(scenario: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to exercise the review UI."
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[1];
class ElementStub {
  constructor(id = '', tag = 'DIV') {
    this.id = id; this.value = ''; this.hidden = false; this.textContent = '';
    this.tagName = tag; this.disabled = false; this.attributes = {}; this.focusCount = 0; this.scrollCount = 0;
    this.listeners = {}; this.children = []; this.classList = { add() {} };
    this.elements = new Proxy({}, { get(target, key) {
      return target[key] ||= new ElementStub(String(key), 'INPUT');
    }});
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return name === 'content' ? 'test-token' : this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  replaceChildren() { this.children = []; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  querySelector() { return this.children.find(child => child.attributes['aria-selected'] === 'true') || null; }
  querySelectorAll() {
    const descendants = [...this.children, ...Object.values(this.elements)];
    return descendants.flatMap(child => [
      ...(['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(child.tagName) ? [child] : []),
      ...child.querySelectorAll(),
    ]);
  }
  contains(child) { return this.children.includes(child); }
  closest() { return null; }
  focus() {
    assert.equal(this.disabled, false, 'A disabled control cannot receive focus');
    this.focusCount += 1; document.activeElement = this;
  }
  scrollIntoView() { this.scrollCount += 1; }
}
const elements = new Map();
function get(id) {
  if (!elements.has(id)) elements.set(id, new ElementStub(id));
  return elements.get(id);
}
const document = {
  getElementById: get, querySelector: get,
  createElement: tag => new ElementStub('', tag.toUpperCase()),
  addEventListener(type, callback) { this[type] = callback; },
};
const filter = get('review-filter');
filter.elements.scope = get('review-scope');
filter.elements.status = get('review-status');
filter.elements.q = get('review-query');
filter.elements.scope.tagName = 'INPUT';
filter.elements.status.tagName = 'SELECT';
filter.elements.q.tagName = 'INPUT';
filter.elements.scope.value = 'identity';
filter.elements.status.value = 'pending';
const form = get('review-edit-form');
const merge = get('review-merge-form');
const workspace = get('.review-workspace');
const submit = get('review-edit-submit');
const filterSubmit = get('review-filter-submit');
const mergeSubmit = get('review-merge-submit');
const approve = get('review-approve');
const retry = get('review-retry');
const loadMore = get('review-load-more');
const defer = get('review-defer');
const reject = get('review-reject');
[submit, filterSubmit, mergeSubmit, approve, retry, loadMore, defer, reject].forEach(control => { control.tagName = 'BUTTON'; });
submit.textContent = '修正して確認を終える'; filterSubmit.textContent = '絞り込む';
mergeSubmit.textContent = '統合する'; retry.textContent = '再試行';
filter.append(filterSubmit);
const identityScope = get('review-scope-identity');
const metadataScope = get('review-scope-metadata');
identityScope.tagName = 'BUTTON'; metadataScope.tagName = 'BUTTON';
filter.append(identityScope, metadataScope);
form.append(submit);
merge.append(mergeSubmit);
workspace.append(form, merge, get('review-queue'), get('review-candidates'), approve, defer, reject, loadMore);
get('review-error').append(retry);
class FormDataStub {
  constructor(form) { this.form = form; }
  get(key) { return this.form.elements[key].value; }
  forEach(callback) {
    for (const [key, field] of Object.entries(this.form.elements)) {
      if (!field.disabled) callback(field.value, key);
    }
  }
}
const items = [123, 124].map(id => ({
  id, version: 4, extracted_name: 'Original ' + id, extracted_area: '銀座',
  extracted_category: '寿司', assets: [], candidates: [], review_group_mention_ids: [id],
  review_group_count: 1, shop: { shop_name: 'Original ' + id, branch_name: 'Original branch',
    area: '銀座', category: '寿司', address: 'Original address', version: 7 },
}));
const queuePayload = {
  scope: 'identity', counters: { pending: 2, approved: 0, deferred: 0, source_unavailable: 0, failed: 0 },
  items, total_count: 2, next_cursor: null,
};
if (scenario === 'candidate_draft') {
  items[0].candidates = [{ id: 42, name: 'Candidate shop', provenance: 'web_search', name_similarity: 0.95 }];
}
if (scenario === 'metadata_branch') {
  items[0].shop.branch_name = null;
  items[0].extracted_branch_name = 'Unconfirmed branch';
}
if (scenario === 'reasons_and_counts') {
  items[0].difference_type = 'new_ambiguous';
  items[1].difference_type = 'custom_csv_reason';
  items[0].fetch_error = 'Original fetch failed';
  items[0].extraction_source = 'legacy_import';
  queuePayload.counters.source_unavailable = 5;
  queuePayload.counters.failed = 3;
}
if (scenario === 'last_item') {
  items.splice(1);
  queuePayload.total_count = 1;
}
if (scenario === 'cannot_approve') {
  items[0].shop = null;
  items[0].difference_type = 'extraction_not_found';
  items[0].extracted_name = '（店舗名未特定）';
}
const calls = [];
let postCount = 0;
let getCount = 0;
let releasePost;
let releaseGet;
let confirmResult = false;
const confirmations = [];
const response = (status, payload) => ({ ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(payload)) });
async function fetchRequest(url, options) {
  calls.push({ url, method: options.method, body: options.body });
  if (options.method === 'POST') {
    postCount += 1;
    if (['busy', 'reject'].includes(scenario) && postCount === 1) {
      await new Promise(resolve => { releasePost = resolve; });
      return response(500, { detail: 'Save unavailable' });
    }
    if (postCount === 1 && scenario === 'network') throw new Error('Connection lost');
    if (postCount === 1 && scenario === 'server') return response(500, { detail: 'Save unavailable' });
    if (scenario === 'conflict') return response(409, { detail: 'Version changed' });
    if (scenario.startsWith('draft_')) {
      const payload = JSON.parse(options.body);
      if (payload.expected_version !== items[0].version || payload.shop_version !== items[0].shop.version) {
        return response(409, { detail: 'Version changed' });
      }
    }
    return response(200, { automatically_resolved_mention_ids: [], automatically_resolved_count: 0 });
  }
  getCount += 1;
  if (['scope_switch', 'scope_failure'].includes(scenario) && getCount === 2) {
    await new Promise(resolve => { releaseGet = resolve; });
    if (scenario === 'scope_failure') return response(503, { detail: 'Scope unavailable' });
  }
  if (scenario === 'busy' && getCount === 2) await new Promise(resolve => { releaseGet = resolve; });
  if (scenario === 'failed_filters' && [2, 4].includes(getCount)) return response(503, { detail: 'Queue unavailable' });
  if (scenario === 'queue_focus' && getCount === 5) return response(503, { detail: 'Queue unavailable' });
  if (scenario === 'draft_reload_conflict' && getCount === 3) return response(503, { detail: 'Queue unavailable' });
  if (getCount === 2 && scenario === 'counters') return response(500, { detail: 'Count unavailable' });
  const params = new URLSearchParams(url.split('?')[1]);
  if (scenario === 'last_item' && postCount > 0) return response(200, { ...queuePayload, items: [], total_count: 0 });
  if (scenario === 'queue_focus' && params.get('q') === 'empty') return response(200, { ...queuePayload, items: [], total_count: 0 });
  if (scenario === 'reasons_and_counts' && params.get('q') === 'target') {
    return response(200, { ...queuePayload, items: [items[1]], total_count: 1 });
  }
  if (scenario === 'draft_removed_conflict' && postCount > 0) {
    return response(200, { ...queuePayload, items: [items[1]], total_count: 1 });
  }
  return response(200, { ...queuePayload, scope: params.get('scope') || 'identity' });
}
vm.runInNewContext(fs.readFileSync('web/static/js/review.js', 'utf8'), {
  document, FormData: FormDataStub, URLSearchParams, fetch: fetchRequest,
  Element: ElementStub, window: {
    prompt() { assert.fail('Reject must not request a reason'); },
    confirm(message) { confirmations.push(message); return confirmResult; },
  }, console,
});
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await flush();
  assert.equal(filter.disabled, false);
  assert.equal(filter.elements.scope.disabled, false);
  assert.equal(filterSubmit.textContent, '絞り込む');
  const key = value => document.keydown({ key: value, target: workspace, preventDefault() {} });
  if (scenario === 'reject') {
    form.elements.shop_name.value = 'Unsaved draft'; form.listeners.input();
    reject.listeners.click.call(reject);
    assert.equal(postCount, 1, 'Reject must send the decision immediately');
    assert.equal(reject.disabled, true);
    assert.equal(reject.getAttribute('aria-busy'), 'true');
    reject.listeners.click.call(reject);
    assert.equal(postCount, 1, 'Repeated clicks must not send another decision');
    const firstPost = calls.find(call => call.method === 'POST');
    assert.equal(firstPost.url, '/api/admin/reviews/123/decision');
    assert.deepEqual(JSON.parse(firstPost.body), {
      action: 'reject', expected_version: 4, scope: 'identity', shop_version: 7,
    });
    releasePost(); await flush();
    assert.equal(getCount, 1, 'A failed decision must not reload the queue');
    assert.equal(get('review-queue').children.length, 2);
    assert.equal(form.elements.shop_name.value, 'Unsaved draft');
    assert.equal(get('review-error').hidden, false);
    assert.equal(reject.disabled, false);
    retry.listeners.click(); await flush();
    const posts = calls.filter(call => call.method === 'POST');
    assert.equal(posts.length, 2);
    assert.deepEqual(posts[1], firstPost, 'Retry must repeat the same decision');
    assert.equal(confirmations.length, 0, 'Reject and retry must not open a dialog');
    assert.equal(get('review-error').hidden, true);
    assert.equal(get('review-queue').children.length, 1);
    assert.equal(form.elements.shop_name.value, 'Original 124');
    assert.equal(document.activeElement, get('review-queue').children[0]);
    return;
  }
  if (scenario.startsWith('draft_')) {
    form.elements.shop_name.value = 'Draft shop name'; form.listeners.input();
    if (scenario === 'draft_scope_conflict') {
      metadataScope.listeners.click(); await flush();
    }
    items[0] = { ...items[0], version: 5, shop: {
      ...items[0].shop, version: 8, address: 'Saved in another tab',
    } };
    if (scenario === 'draft_scope_conflict') identityScope.listeners.click();
    else filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(form.elements.shop_name.value, 'Draft shop name');
    assert.equal(form.elements.address.value, 'Original address');
    form.elements.phone.value = '03-1111-2222'; form.listeners.input();
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    const originalPost = JSON.parse(calls.find(call => call.method === 'POST').body);
    assert.equal(originalPost.expected_version, 4);
    assert.equal(originalPost.shop_version, 7);
    assert.equal(originalPost.shop.address, 'Original address');
    assert.equal(form.elements.shop_name.value, 'Draft shop name');
    assert.equal(form.elements.phone.value, '03-1111-2222');
    assert.match(get('review-error-text').textContent, /別の更新/);
    assert.equal(retry.textContent, '最新情報を読み込む');
    assert.equal(document.activeElement, get('review-error'));
    const callsBeforeCancel = calls.length;
    retry.listeners.click(); await flush();
    assert.equal(calls.length, callsBeforeCancel);
    assert.equal(form.elements.shop_name.value, 'Draft shop name');
    assert.match(confirmations[0], /未保存の入力を破棄/);
    confirmResult = true;
    retry.listeners.click(); await flush();
    if (scenario === 'draft_reload_conflict') {
      assert.equal(form.elements.shop_name.value, 'Draft shop name');
      assert.equal(form.elements.phone.value, '03-1111-2222');
      assert.match(get('review-error-text').textContent, /入力内容は保持/);
      assert.equal(postCount, 1);
      retry.listeners.click(); await flush();
    }
    assert.equal(postCount, 1, 'Resolving a conflict must only fetch the latest values');
    if (scenario === 'draft_removed_conflict') {
      assert.equal(form.elements.shop_name.value, 'Original 124');
      assert.equal(get('review-error').hidden, true);
      assert.match(get('review-notice-text').textContent, /絞り込み条件や追加読み込み/);
      assert.equal(approve.disabled, false);
      return;
    }
    assert.equal(form.elements.shop_name.value, 'Original 123');
    assert.equal(form.elements.address.value, 'Saved in another tab');
    assert.equal(get('review-error').hidden, true);
    assert.equal(approve.disabled, false);
    assert.match(get('review-notice-text').textContent, /最新の情報/);
    form.elements.shop_name.value = 'Reapplied name'; form.listeners.input();
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    const latestPost = JSON.parse(calls.filter(call => call.method === 'POST')[1].body);
    assert.equal(latestPost.expected_version, 5);
    assert.equal(latestPost.shop_version, 8);
    assert.equal(latestPost.shop.shop_name, 'Reapplied name');
    assert.equal(latestPost.shop.address, 'Saved in another tab');
    assert.equal(get('review-error').hidden, true);
    return;
  }
  if (scenario === 'last_item') {
    approve.listeners.click(); await flush();
    assert.equal(postCount, 1);
    assert.equal(get('review-queue').children.length, 0);
    assert.equal(get('review-decision').hidden, true);
    assert.equal(get('review-result-count').textContent, '0');
    assert.equal(document.activeElement, filterSubmit);
    assert.match(get('review-notice-text').textContent, /保存しました/);
    return;
  }
  if (['scope_switch', 'scope_failure'].includes(scenario)) {
    form.elements.address.value = 'Saved identity draft'; form.listeners.input();
    filter.elements.status.value = 'rejected';
    metadataScope.listeners.click();
    assert.equal(getCount, 2);
    assert.equal(filter.elements.scope.value, 'identity', 'Switch only after a successful response');
    assert.equal(identityScope.getAttribute('aria-pressed'), 'true');
    assert.equal(metadataScope.disabled, true);
    assert.equal(form.elements.shop_name.readOnly, false);
    const pendingParams = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(pendingParams.get('scope'), 'metadata');
    assert.equal(pendingParams.get('status'), 'pending');
    releaseGet(); await flush();
    if (scenario === 'scope_failure') {
      assert.equal(filter.elements.scope.value, 'identity');
      assert.equal(form.elements.address.value, 'Saved identity draft');
      assert.equal(identityScope.getAttribute('aria-pressed'), 'true');
      assert.equal(get('#review-status option[value="rejected"]').hidden, false);
      retry.listeners.click(); await flush();
    }
    assert.equal(metadataScope.getAttribute('aria-pressed'), 'true');
    assert.equal(identityScope.getAttribute('aria-pressed'), 'false');
    assert.equal(filter.elements.scope.value, 'metadata');
    assert.equal(filter.elements.status.value, 'pending');
    assert.equal(get('#review-status option[value="rejected"]').hidden, true);
    assert.equal(get('#review-status option[value="rejected"]').disabled, true);
    assert.equal(form.elements.shop_name.readOnly, true);
    assert.equal(get('review-reject').hidden, true);
    assert.equal(get('review-candidates').children.length, 0);
    assert.match(get('review-result-context').textContent, /エリア・カテゴリを確認.*未確認/);
    identityScope.listeners.click(); await flush();
    assert.equal(form.elements.address.value, 'Saved identity draft');
    assert.equal(form.elements.shop_name.readOnly, false);
    assert.equal(get('#review-status option[value="rejected"]').hidden, false);
    assert.equal(postCount, 0);
    return;
  }
  if (scenario === 'candidate_draft') {
    const button = get('review-candidates').children[0].children.find(child => child.tagName === 'BUTTON');
    assert.match(button.textContent, /保存して確認を終える/);
    form.elements.shop_name.value = 'Edited draft'; form.listeners.input();
    assert.equal(button.disabled, true);
    button.listeners.click(); await flush();
    assert.equal(postCount, 0);
    assert.equal(form.elements.shop_name.value, 'Edited draft');
    form.elements.shop_name.value = 'Original 123'; form.listeners.input();
    assert.equal(button.disabled, false);
    button.listeners.click(); await flush();
    const payload = JSON.parse(calls.find(call => call.method === 'POST').body);
    assert.equal(payload.action, 'approve_candidate');
    assert.equal(payload.candidate_id, 42);
    return;
  }
  if (scenario === 'metadata_branch') {
    metadataScope.listeners.click(); await flush();
    assert.equal(form.elements.branch_name.value, '');
    assert.equal(form.elements.branch_name.readOnly, true);
    form.elements.area.value = '新宿'; form.listeners.input();
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    const payload = JSON.parse(calls.find(call => call.method === 'POST').body);
    assert.equal(payload.scope, 'metadata');
    assert.equal(payload.shop.branch_name, null, 'Do not send an extracted branch as a metadata edit');
    assert.equal(payload.shop.area, '新宿');
    return;
  }
  if (scenario === 'reasons_and_counts') {
    assert.equal(get('review-result-count').textContent, '2');
    assert.equal(get('count-unavailable').textContent, 5);
    assert.equal(get('count-failed').textContent, 3);
    assert.equal(get('review-difference-code').textContent, 'new_ambiguous');
    assert.doesNotMatch(get('review-difference').textContent, /new_ambiguous/);
    assert.equal(get('review-extraction-source').textContent, 'legacy_import');
    assert.equal(get('review-source-warning').hidden, false);
    get('review-queue').children[1].listeners.click();
    assert.equal(get('review-difference-code').textContent, 'custom_csv_reason');
    assert.doesNotMatch(get('review-difference').textContent, /custom_csv_reason/);
    assert.match(get('review-difference').textContent, /処理情報/);
    assert.equal(get('review-source-warning').hidden, true);
    filter.elements.q.value = 'target';
    filter.elements.status.value = 'approved';
    filter.listeners.submit({ preventDefault() {} }); await flush();
    assert.equal(get('review-result-count').textContent, '1');
    assert.match(get('review-result-context').textContent, /店・支店を確認.*確認済み.*target/);
    assert.equal(get('count-unavailable').textContent, 5);
    return;
  }
  if (scenario === 'queue_focus') {
    assert.equal(document.activeElement, undefined, 'Initial loading must not move focus');
    filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(document.activeElement, get('review-queue').children[0]);
    loadMore.listeners.click();
    await flush();
    assert.equal(document.activeElement, get('review-queue').children[0]);
    filter.elements.q.value = 'empty';
    filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(document.activeElement, filterSubmit);
    filter.elements.q.value = 'retry';
    filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(document.activeElement, get('review-error'));
    retry.listeners.click();
    await flush();
    assert.equal(document.activeElement, get('review-queue').children[0]);
    return;
  }
  if (scenario === 'cannot_approve') {
    assert.equal(approve.disabled, true);
    filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(approve.disabled, true);
    approve.listeners.click(); key('a');
    await flush();
    assert.equal(postCount, 0);
    return;
  }
  assert.equal(approve.disabled, false);
  if (scenario === 'dirty') {
    form.elements.shop_name.value = 'Edited draft'; form.listeners.input();
    assert.equal(approve.disabled, true);
    approve.listeners.click(); key('a');
    await flush();
    assert.equal(postCount, 0);
    get('review-queue').children[1].listeners.click();
    assert.equal(approve.disabled, false);
    get('review-queue').children[0].listeners.click();
    assert.equal(form.elements.shop_name.value, 'Edited draft');
    assert.equal(approve.disabled, true);
    filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(approve.disabled, true);
    form.elements.shop_name.value = 'Original 123'; form.listeners.input();
    assert.equal(approve.disabled, false);
    key('a');
    await flush();
    assert.equal(postCount, 1);
    assert.equal(JSON.parse(calls.find(call => call.method === 'POST').body).action, 'approve_current');
    return;
  }
  if (scenario === 'filters') {
    filter.elements.status.value = 'approved';
    filter.elements.q.value = 'submitted query';
    assert.equal(getCount, 1);
    metadataScope.listeners.click();
    await flush();
    let params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('scope'), 'metadata');
    assert.equal(params.get('status'), 'approved');
    assert.equal(params.get('q'), 'submitted query');
    assert.equal(form.elements.shop_name.readOnly, true);
    filter.elements.status.value = 'deferred';
    filter.elements.q.value = 'pending query';
    assert.equal(form.elements.shop_name.readOnly, true);
    loadMore.listeners.click();
    await flush();
    params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('scope'), 'metadata');
    assert.equal(params.get('status'), 'approved');
    assert.equal(params.get('q'), 'submitted query');
    identityScope.listeners.click();
    await flush();
    params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('scope'), 'identity');
    assert.equal(params.get('status'), 'deferred');
    assert.equal(params.get('q'), 'pending query');
    filter.elements.status.value = 'rejected';
    metadataScope.listeners.click();
    await flush();
    params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('status'), 'pending');
    assert.equal(getCount, 5);
    return;
  }
  if (scenario === 'failed_filters') {
    filter.elements.status.value = 'approved';
    filter.elements.q.value = 'first submitted query';
    metadataScope.listeners.click();
    await flush();
    assert.equal(form.elements.shop_name.readOnly, false);
    assert.equal(get('review-error').hidden, false);
    filter.elements.status.value = 'deferred';
    filter.elements.q.value = 'not submitted yet';
    retry.listeners.click();
    await flush();
    let params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('scope'), 'metadata');
    assert.equal(params.get('status'), 'approved');
    assert.equal(params.get('q'), 'first submitted query');
    assert.equal(form.elements.shop_name.readOnly, true);
    filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(get('review-error').hidden, false);
    assert.equal(form.elements.shop_name.readOnly, true);
    loadMore.listeners.click();
    await flush();
    params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('scope'), 'metadata');
    assert.equal(params.get('status'), 'approved');
    assert.equal(params.get('q'), 'first submitted query');
    form.elements.area.value = '新宿'; form.listeners.input();
    form.listeners.submit({ preventDefault() {}, currentTarget: form });
    await flush();
    params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('scope'), 'metadata');
    assert.equal(params.get('status'), 'approved');
    assert.equal(params.get('q'), 'first submitted query');
    assert.equal(JSON.parse(calls.find(call => call.method === 'POST').body).scope, 'metadata');
    return;
  }
  const edit = { shop_name: 'Edited name', branch_name: 'Edited branch', address: 'Edited address', phone: '' };
  Object.entries(edit).forEach(([key, value]) => { form.elements[key].value = value; });
  form.listeners.input();
  if (scenario === 'reload') {
    get('review-filter').listeners.submit({ preventDefault() {} });
    await flush();
    Object.entries(edit).forEach(([key, value]) => assert.equal(form.elements[key].value, value));
    assert.equal(postCount, 0);
    assert.equal(approve.disabled, true);
    return;
  }
  if (scenario === 'busy') {
    merge.elements.rating.disabled = true;
    form.listeners.submit({ preventDefault() {}, currentTarget: form });
    assert.equal(submit.textContent, '保存中…');
    assert.equal(submit.getAttribute('aria-busy'), 'true');
    assert.equal(workspace.getAttribute('aria-busy'), 'true');
    assert.equal(form.elements.shop_name.disabled, true);
    assert.equal(filter.elements.scope.disabled, true);
    assert.equal(approve.disabled, true);
    assert.equal(retry.disabled, true);
    assert.equal(get('review-queue').children[0].disabled, true);
    form.listeners.submit({ preventDefault() {}, currentTarget: form });
    approve.listeners.click(); filter.listeners.submit({ preventDefault() {} }); key('a');
    assert.equal(postCount, 1);
    releasePost(); await flush();
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(filter.elements.scope.disabled, false);
    assert.equal(submit.textContent, '修正して確認を終える');
    assert.equal(submit.getAttribute('aria-busy'), undefined);
    assert.equal(retry.disabled, false);
    assert.equal(approve.disabled, true);
    assert.equal(merge.elements.rating.disabled, true);
    assert.equal(get('review-error').focusCount, 1);
    assert.equal(get('review-error').scrollCount, 1);
    retry.listeners.click(); await flush();
    assert.equal(postCount, 2);
    assert.equal(form.elements.shop_name.disabled, true);
    assert.equal(get('review-queue').children[0].disabled, true);
    releaseGet(); await flush();
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(retry.disabled, false);
    assert.equal(approve.disabled, false);
    assert.equal(merge.elements.rating.disabled, true);
    assert.equal(get('review-queue').children[0].focusCount, 1);
    const posts = calls.filter(call => call.method === 'POST');
    assert.equal(posts[0].body, posts[1].body);
    return;
  }
  if (scenario === 'counters') {
    filter.elements.status.value = 'approved';
    filter.elements.q.value = 'not submitted';
  }
  form.listeners.submit({ preventDefault() {}, currentTarget: form });
  await flush();
  assert.equal(postCount, 1);
  if (scenario !== 'counters') {
    Object.entries(edit).forEach(([key, value]) => assert.equal(form.elements[key].value, value));
    assert.equal(getCount, 1);
    assert.match(get('review-error-text').textContent, /入力内容は保持/);
    assert.equal(approve.disabled, true);
    assert.equal(get('review-error').focusCount, 1);
    assert.equal(get('review-error').scrollCount, 1);
    if (scenario === 'conflict') {
      assert.match(get('review-error-text').textContent, /別の更新/);
      assert.equal(retry.textContent, '最新情報を読み込む');
      retry.listeners.click(); await flush();
      assert.equal(getCount, 1);
      assert.equal(postCount, 1);
      Object.entries(edit).forEach(([key, value]) => assert.equal(form.elements[key].value, value));
      confirmResult = true;
      retry.listeners.click(); await flush();
      assert.equal(getCount, 2);
      assert.equal(postCount, 1);
      assert.equal(form.elements.shop_name.value, 'Original 123');
      assert.equal(get('review-error').hidden, true);
      return;
    }
  } else {
    assert.match(get('review-error-text').textContent, /保存は完了/);
    form.elements.shop_name.value = 'Next item draft';
  }
  get('review-retry').listeners.click();
  await flush();
  const posts = calls.filter(call => call.method === 'POST');
  if (scenario === 'counters') {
    assert.equal(postCount, 1);
    assert.equal(getCount, 3);
    assert.equal(form.elements.shop_name.value, 'Next item draft');
    assert.equal(get('review-error').hidden, true);
    const params = new URLSearchParams(calls.at(-1).url.split('?')[1]);
    assert.equal(params.get('scope'), 'identity');
    assert.equal(params.get('status'), 'pending');
    assert.equal(params.get('q'), null);
  } else {
    assert.equal(postCount, 2);
    assert.equal(posts[0].url, posts[1].url);
    assert.equal(posts[0].body, posts[1].body);
    const payload = JSON.parse(posts[1].body);
    assert.equal(payload.expected_version, 4);
    assert.equal(payload.shop_version, 7);
    assert.equal(payload.shop.shop_name, 'Edited name');
    assert.equal(get('review-error').hidden, true);
    assert.equal(get('review-queue').children.length, 1);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", harness, scenario], cwd=PROJECT_ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
