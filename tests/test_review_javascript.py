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
    "photo_preview_only", "photo_browse_selected", "photo_uncheck", "photo_single", "photo_preview_error", "photo_selected_error", "photo_readonly", "photo_busy", "photo_save_failure", "photo_scope",
    "manual_queue_draft", "save_then_complement", "initial_complement", "mobile_complement", "complement_failure", "complement_ambiguous", "complement_no_empty", "complement_repeat", "photo_carousel", "photo_error", "photo_retry", "photo_drafts", "photo_existing", "readonly_navigation",
    "ai_choices", "ai_conflict", "all_remaining", "server", "network", "conflict", "counters", "reload", "dirty", "busy", "filters", "failed_filters", "cannot_approve", "queue_focus",
    "scope_switch", "scope_failure", "candidate_draft", "metadata_branch", "reasons_and_counts", "last_item",
    "draft_reload_conflict", "draft_scope_conflict", "draft_removed_conflict", "reject",
    "reject_cancel", "reject_shortcut", "reject_unlinked", "reject_unlinked_draft", "reject_metadata", "reject_empty",
    "supplement_inline", "supplement_drafts", "supplement_retry", "supplement_retry_edit",
    "shared_shop", "shared_shop_draft", "overlap_mine", "overlap_latest", "overlap_repeat", "collision", "validation",
    "missing_shop", "merge_target",
    "response_lost", "event_reason", "event_origin_error",
    "link_linked", "link_unlinked", "link_preview_target", "link_preview_selection",
    "link_preview_filter", "link_preview_scope", "link_dirty", "link_other_scope_draft",
    "link_network", "link_conflict", "link_bad_mention", "link_bad_target", "link_note_change",
    "link_newer_mention", "link_newer_source", "link_changed_source", "link_source_sync", "link_source_draft",
])
def test_review_retry_preserves_edits_and_repeats_only_the_failed_request(scenario: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to exercise the review UI."
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[2];
class ElementStub {
  constructor(id = '', tag = 'DIV') {
    this.id = id; this.value = ''; this.hidden = false; this.textContent = '';
    this.tagName = tag; this.disabled = false; this.attributes = {}; this.focusCount = 0; this.scrollCount = 0;
    this.listeners = {}; this.children = []; this.dataset = {};
    const classes = new Set();
    this.classList = { add(value) { classes.add(value); }, remove(value) { classes.delete(value); }, contains(value) { return classes.has(value); } };
    this.elements = new Proxy({}, { get(target, key) {
      return target[key] ||= new ElementStub(String(key), 'INPUT');
    }});
  }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return name === 'content' ? 'test-token' : this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; }
  replaceChildren(...children) { this.children = children; }
  get firstElementChild() { return this.children[0] || null; }
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
  click() {
    if (this.disabled) return;
    this.listeners.click?.call(this);
    if (this.form) this.form.listeners.submit?.({ preventDefault() {}, currentTarget: this.form });
  }
  focus() {
    assert.equal(this.disabled, false, 'A disabled control cannot receive focus');
    this.focusCount += 1; document.activeElement = this;
  }
  scrollIntoView() { this.scrollCount += 1; }
  reportValidity() { return true; }
}
const elements = new Map();
function get(id) {
  if (!elements.has(id)) elements.set(id, new ElementStub(id));
  return elements.get(id);
}
const document = {
  getElementById: get, querySelector: get,
  createElement: tag => new ElementStub('', tag.toUpperCase()),
  createTextNode: text => { const node = new ElementStub('', '#TEXT'); node.textContent = text; return node; },
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
const link = get('review-link-form');
link.elements.target_shop_id = get('review-link-target');
link.elements.note = get('review-link-note');
link.elements.target_shop_id.tagName = 'INPUT'; link.elements.note.tagName = 'TEXTAREA';
const linkPreview = get('review-link-preview-submit');
const linkApply = get('review-link-apply');
const linkConfirm = get('review-link-confirm');
linkPreview.tagName = 'BUTTON'; linkApply.tagName = 'BUTTON'; linkConfirm.tagName = 'INPUT';
link.append(linkPreview, linkConfirm, linkApply);
const workspace = get('.review-workspace');
const submit = get('review-edit-submit');
const filterSubmit = get('review-filter-submit');
const mergeSubmit = get('review-merge-submit');
get('review-photo-error').hidden = true;
submit.form = form;
const ai = get('review-ai-search'); ai.tagName = 'BUTTON';
const photoSelect = get('review-photo-select'); photoSelect.tagName = 'INPUT'; form.append(photoSelect);
const supplement = get('review-supplement'); supplement.tagName = 'TEXTAREA';
const retry = get('review-retry');
const loadMore = get('review-load-more');
const defer = get('review-defer');
const deferNote = get('review-defer-note');
deferNote.tagName = 'TEXTAREA';
const reject = get('review-reject');
[submit, filterSubmit, mergeSubmit, retry, loadMore, defer, reject].forEach(control => { control.tagName = 'BUTTON'; });
submit.textContent = '登録'; filterSubmit.textContent = '絞り込む';
mergeSubmit.textContent = '統合する'; retry.textContent = '再試行';
filter.append(filterSubmit);
const identityScope = get('review-scope-identity');
const metadataScope = get('review-scope-metadata');
identityScope.tagName = 'BUTTON'; metadataScope.tagName = 'BUTTON';
filter.append(identityScope, metadataScope);
form.append(submit);
merge.append(mergeSubmit);
workspace.append(form, merge, link, get('review-queue'), get('review-candidates'), ai, supplement, reject, loadMore);
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
  extracted_category: '寿司', extracted_branch_name: 'Original branch', review_status: 'pending', metadata_review_status: 'pending', assets: [], candidates: [], review_group_mention_ids: [id],
  review_group_count: 1, shop: { id: id + 100, shop_name: 'Original ' + id, branch_name: 'Original branch',
    area: '銀座', category: '寿司', address: 'Original address', version: 7 },
}));
if (scenario === 'all_remaining') {
  filter.elements.scope.value = 'all';
  items.forEach(item => { item.review_status = 'pending'; item.metadata_review_status = 'pending'; });
}
const queuePayload = {
  scope: 'identity', counters: { unresolved: 2, pending: 2, approved: 0, deferred: 0, source_unavailable: 0, failed: 0 },
  items, total_count: 2, next_cursor: null,
};
if (scenario.startsWith('shared_shop')) {
  items[1].shop = { ...items[0].shop };
}
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
if (scenario === 'event_reason') {
  items[0].difference_type = 'event_excluded';
  items[0].review_status = 'rejected';
  items[0].shop = null;
  items[0].extraction_error = '出店元の支店を特定できません。';
  items[0].candidates = [{ id: 42, name: 'Venue candidate', provenance: 'web_search', name_similarity: 0.99 }];
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
if (scenario === 'link_unlinked') items[0].shop = null;
if (scenario === 'reject_unlinked' || scenario === 'reject_unlinked_draft') items[0].shop = null;
if (scenario === 'reject_empty') { items.splice(0); queuePayload.total_count = 0; }
if (scenario === 'link_source_sync' || scenario === 'link_source_draft') items[1].shop = { ...items[0].shop };
if (['initial_complement', 'mobile_complement'].includes(scenario)) {
  items[0].shop.category = null;
}
if (scenario === 'save_then_complement') { filter.elements.scope.value = 'all'; items[1].shop.category = null; }
if (scenario.startsWith('photo_')) {
  items[0].assets = [1, 2].map(id => ({id, kind: 'image', photo_url: '/api/admin/evidence/photo/' + id}));
  if (scenario === 'photo_existing' || scenario === 'photo_readonly') items[0].shop.image_key = 'a'.repeat(64);
  if (scenario === 'photo_single') items[0].assets.splice(1);
  if (scenario === 'photo_readonly') workspace.dataset.readOnly = 'true';
}
if (scenario === 'readonly_navigation') workspace.dataset.readOnly = 'true';
const targetShop = { id: 900, version: 11, shop_name: 'Target shop', branch_name: '本店', area: '神田',
  is_visited: true, visited_at: '2026-03-01', rating: 4, memo: '<b>Keep this memo</b>', category: '寿司' };
const calls = [];
let postCount = 0;
let getCount = 0;
let releasePost;
let releaseGet;
let releasePreview;
let releaseAi;
let linkPreviewCount = 0;
let confirmResult = false;
const confirmations = [];
const response = (status, payload) => ({ ok: status < 400, status, json: async () => JSON.parse(JSON.stringify(payload)) });
async function fetchRequest(url, options) {
  calls.push({ url, method: options.method, body: options.body });
  if (options.method === 'GET') assert.equal(options.cache, 'no-store');
  if (url.includes('/link-preview?')) {
    linkPreviewCount += 1;
    const preview = {
      mention_id: scenario === 'link_bad_mention' ? 124 : 123,
      expected_version: scenario === 'link_newer_mention' && linkPreviewCount === 1 ? 5 : items[0].version,
      source_shop: items[0].shop ? { ...items[0].shop,
        id: scenario === 'link_changed_source' && linkPreviewCount === 1 ? 999 : items[0].shop.id,
        version: scenario === 'link_newer_source' && linkPreviewCount === 1 ? 8 : items[0].shop.version, memo: 'Source memo' } : null,
      target_shop: { ...targetShop, id: scenario === 'link_bad_target' ? 901 : Number(new URLSearchParams(url.split('?')[1]).get('target_shop_id')) },
      source_mention_count: items[0].shop ? 1 : 0, target_mention_count: 3,
      source_will_be_empty: Boolean(items[0].shop), review_status: 'pending', metadata_review_status: 'deferred',
    };
    if (scenario.startsWith('link_preview_') && linkPreviewCount === 1) {
      await new Promise(resolve => { releasePreview = resolve; });
    }
    return response(200, preview);
  }
  if (options.method === 'POST') {
    postCount += 1;
    if (url.endsWith('/suggestions')) {
      assert.equal(options.headers['Content-Type'], 'application/json');
      const body = JSON.parse(options.body);
      if (scenario === 'photo_busy') {
        await new Promise(resolve => { releaseAi = resolve; });
        return response(200, {candidates: [{shop_name: 'Changed name', branch_name: 'New branch', area: '神田', category: '食堂', evidence_urls: [], reason: 'Source'}], unresolved_reason: null});
      }
      if (scenario === 'save_then_complement') {
        assert.equal(body.draft.shop_name, 'Original 124');
        await new Promise(resolve => { releaseAi = resolve; });
        return response(200, {candidates: [{shop_name: 'Different shop', area: '神田', category: '寿司', branch_name: 'Different branch', evidence_urls: [], reason: 'Source'}], unresolved_reason: null});
      }
      if (scenario.includes('complement')) {
        assert.equal(body.draft.shop_name, 'Original 123');
        assert.equal(body.draft.area, '銀座', 'Filled fields are sent as context');
        if (['initial_complement', 'mobile_complement'].includes(scenario)) await new Promise(resolve => { releaseAi = resolve; });
        if (scenario === 'complement_failure') return response(502, {detail: 'AI unavailable'});
        const candidate = {shop_name: 'Different name', branch_name: 'Different branch', area: '神田', category: '寿司', evidence_urls: ['https://example.com/shop'], reason: 'Source'};
        if (scenario === 'complement_repeat') return response(200, {candidates: [], unresolved_reason: 'Not found'});
        return response(200, {candidates: scenario === 'complement_ambiguous' ? [candidate, {...candidate, category: '中華'}] : [candidate], unresolved_reason: null});
      }
      assert.equal(body.draft.shop_name, 'My edited name');
      assert.equal(body.draft.phone, '');
      assert.equal(body.supplement, 'Near the station');
      assert.equal(body.reference_url, 'https://example.com/reference');
      assert.equal(body.expected_version, 4);
      assert.equal(body.shop_version, 7);
      if (scenario === 'ai_conflict') {
        items[0].version = 5;
        return response(409, { detail: { code: 'stale_mention', message: 'Version changed' } });
      }
      return response(200, { candidates: [{ shop_name: 'Suggested name', area: '神田', category: '寿司', branch_name: 'Different branch', phone: '0312345678',
        evidence_urls: ['https://example.com/shop'], reason: 'Official source' }], unresolved_reason: null });
    }
    if (scenario === 'save_then_complement') {
      const body = JSON.parse(options.body);
      assert.equal(body.confirm_metadata, true);
      items.shift(); queuePayload.total_count = 1; queuePayload.counters.unresolved = 1;
      return response(200, {automatically_resolved_mention_ids: [], automatically_resolved_count: 0});
    }
    if (scenario === 'all_remaining') {
      assert.equal(JSON.parse(options.body).scope, 'identity');
      assert.equal(JSON.parse(options.body).confirm_metadata, true);
      items[0].review_status = 'approved'; items[0].metadata_review_status = 'approved'; items[0].version = 5;
      return response(200, { shop: items[0].shop, automatically_resolved_mention_ids: [], automatically_resolved_count: 0 });
    }
    if (scenario.startsWith('link_') && url.endsWith('/link')) {
      assert.equal(url, '/api/admin/reviews/123/link');
      if (scenario === 'link_network' && postCount === 1) throw new Error('Response lost after commit');
      if (scenario === 'link_conflict' || scenario === 'link_network') {
        return response(409, { detail: { code: 'stale_shop', message: 'Target changed' } });
      }
      if (postCount === 1) await new Promise(resolve => { releasePost = resolve; });
      return response(200, { shop: targetShop, source_shop: items[0].shop ? { ...items[0].shop, version: 8 } : null,
        automatically_resolved_mention_ids: [], automatically_resolved_count: 0 });
    }
    if (scenario === 'response_lost') {
      const payload = JSON.parse(options.body);
      if (postCount === 1) {
        items[0] = { ...items[0], version: 5, review_status: 'approved', shop: {
          ...items[0].shop, ...payload.shop, version: 8,
        } };
        throw new Error('Response lost after commit');
      }
      assert.equal(payload.expected_version, 4);
      assert.equal(payload.shop_version, 7);
      return response(409, { detail: { code: 'stale_mention', message: 'Already applied' } });
    }
    if (['busy', 'reject', 'supplement_retry', 'supplement_retry_edit'].includes(scenario) && postCount === 1) {
      await new Promise(resolve => { releasePost = resolve; });
      return response(500, { detail: 'Save unavailable' });
    }
    if (postCount === 1 && scenario === 'network') throw new Error('Connection lost');
    if (postCount === 1 && ['server', 'photo_save_failure'].includes(scenario)) return response(500, { detail: 'Save unavailable' });
    if (scenario === 'conflict' || (scenario.startsWith('overlap_') && postCount === 1) || (scenario === 'overlap_repeat' && postCount === 2)) {
      return response(409, { detail: { code: 'stale_shop', message: 'Version changed' } });
    }
    if (scenario === 'collision') return response(409, { detail: { code: 'shop_collision', message: 'shop_id=999' } });
    if (scenario === 'missing_shop') return response(409, { detail: { code: 'missing_shop', message: 'No shop linked' } });
    if (scenario === 'merge_target') return response(409, { detail: { code: 'stale_merge_target', message: 'Target changed' } });
    if (scenario === 'event_origin_error') return response(409, { detail: { code: 'event_origin_required', message: 'Origin required' } });
    if (scenario === 'validation') return response(422, { detail: [
      { loc: ['body', 'edit_and_approve', 'shop', 'area'], msg: '候補: 神田' },
    ] });
    if (scenario.startsWith('draft_')) {
      const payload = JSON.parse(options.body);
      if (payload.expected_version !== items[0].version || payload.shop_version !== items[0].shop.version) {
        return response(409, { detail: { code: 'stale_shop', message: 'Version changed' } });
      }
    }
    if (scenario.startsWith('shared_shop')) {
      items[0].shop = { ...items[0].shop, ...JSON.parse(options.body).shop, version: 8 };
      items[1].shop = { ...items[0].shop };
      return response(200, { shop: items[0].shop, automatically_resolved_mention_ids: [], automatically_resolved_count: 0 });
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
  if (url === '/api/admin/reviews/123') return response(200, items[0]);
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
  Element: ElementStub, queueMicrotask, window: {
    location: { search: '' },
    matchMedia() { return { matches: ['mobile_complement', 'metadata_branch', 'cannot_approve'].includes(scenario) }; },
    prompt() { assert.fail('Review decisions must not open an input dialog'); },
    alert() { assert.fail('Review feedback must stay inline'); },
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
  if (scenario === 'manual_queue_draft') {
    form.elements.shop_name.value = 'Edited shop';
    form.elements.area.value = '渋谷'; form.listeners.input({target: form.elements.shop_name});
    const selected = get('review-queue').children[0];
    assert.equal(selected.children[0].textContent, 'Edited shop');
    assert.equal(selected.children[1].textContent, '渋谷');
    get('review-back').click();
    get('review-queue').children[1].click();
    get('review-queue').children[0].click();
    assert.equal(form.elements.shop_name.value, 'Edited shop');
    assert.equal(form.elements.area.value, '渋谷');
    assert.equal(postCount, 0, 'Draft changes never save or request AI');
    return;
  }
  if (scenario === 'save_then_complement') {
    submit.click(); await flush(); await flush();
    assert.equal(postCount, 1, 'Opening the next item after registration must not request AI');
    assert.equal(form.elements.shop_name.value, 'Original 124');
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(form.elements.category.disabled, false);
    assert.equal(form.elements.category.value, '');
    assert.equal(submit.disabled, true, 'Missing required values still prevent registration');
    assert.equal(ai.disabled, false);
    ai.click(); await flush();
    assert.equal(postCount, 2, 'Only clicking AI starts complementing the next item');
    assert.equal(form.elements.shop_name.disabled, true, 'Manual AI keeps inputs disabled until it finishes');
    assert.equal(form.elements.category.disabled, true);
    assert.equal(submit.disabled, true);
    assert.equal(ai.disabled, true);
    assert.equal(workspace.getAttribute('aria-busy'), 'true');
    submit.click(); ai.click(); key('a');
    await flush(); assert.equal(postCount, 2);
    releaseAi(); await flush();
    assert.equal(form.elements.category.value, '寿司');
    assert.equal(form.elements.area.value, '銀座');
    assert.equal(form.elements.shop_name.value, 'Original 124');
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(submit.disabled, false);
    assert.equal(postCount, 2);
    assert.equal(calls.filter(call => call.url.endsWith('/decision')).length, 1);
    return;
  }
  if (['initial_complement', 'mobile_complement'].includes(scenario)) {
    if (scenario === 'mobile_complement') {
      assert.equal(postCount, 0, 'A hidden mobile detail must not call AI');
      get('review-queue').children[0].click(); await flush();
      assert.equal(document.activeElement, get('review-current-name'), 'Mobile detail focuses a visible heading');
    }
    assert.equal(postCount, 0, 'Opening a detail must not request AI');
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(form.elements.category.disabled, false);
    assert.equal(supplement.disabled, false);
    assert.equal(submit.disabled, true, 'Required fields remain validated before AI is requested');
    assert.equal(ai.disabled, false);
    get('review-queue').children[1].click(); await flush();
    get('review-queue').children[0].click(); await flush();
    assert.equal(postCount, 0, 'Changing the selected item must not request AI');
    assert.equal(form.elements.category.value, '');
    ai.click(); await flush();
    assert.equal(postCount, 1, 'AI starts only after the explicit button click');
    assert.equal(form.elements.shop_name.disabled, true);
    assert.equal(form.elements.category.disabled, true);
    assert.equal(supplement.disabled, true);
    assert.equal(submit.disabled, true);
    assert.equal(ai.disabled, true);
    assert.equal(workspace.getAttribute('aria-busy'), 'true');
    releaseAi(); await flush();
    assert.equal(form.elements.category.value, '寿司');
    assert.equal(form.elements.shop_name.value, 'Original 123');
    assert.equal(form.elements.branch_name.value, 'Original branch');
    assert.equal(form.elements.area.value, '銀座');
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(supplement.disabled, false);
    assert.equal(submit.disabled, false);
    assert.equal(postCount, 1, 'An AI result never registers data');
    return;
  }
  if (scenario.startsWith('complement_')) {
    if (scenario === 'complement_no_empty') {
      assert.equal(ai.disabled, true); ai.click(); await flush();
      assert.equal(postCount, 0); return;
    }
    form.elements.category.value = ''; form.listeners.input({target: form.elements.shop_name});
    supplement.value = 'Keep the supplement'; supplement.listeners.input();
    ai.click(); await flush();
    assert.equal(postCount, 1);
    assert.equal(form.elements.shop_name.value, 'Original 123');
    assert.equal(form.elements.area.value, '銀座');
    assert.equal(supplement.value, 'Keep the supplement');
    if (scenario === 'complement_failure') {
      assert.equal(form.elements.category.value, '');
      assert.equal(form.elements.category.disabled, false);
      assert.equal(ai.disabled, false);
      assert.equal(submit.disabled, true);
      assert.match(get('review-error-text').textContent, /AI unavailable/);
      form.elements.category.value = '寿司'; form.listeners.input({target: form.elements.shop_name});
      assert.equal(submit.disabled, false); submit.click(); await flush();
      assert.equal(postCount, 2);
    } else if (scenario === 'complement_ambiguous') {
      assert.equal(form.elements.category.value, '', 'Multiple candidates must not be silently selected');
      const choices = get('review-ai-results').children.filter(child => child.tagName === 'DIV');
      assert.equal(choices.length, 2);
      choices[1].children[0].click();
      assert.equal(form.elements.category.value, '中華');
      assert.equal(form.elements.shop_name.value, 'Original 123');
      assert.equal(submit.disabled, false);
      assert.equal(postCount, 1);
    } else {
      assert.equal(form.elements.category.value, '');
      await flush(); assert.equal(postCount, 1, 'An unresolved result cannot trigger another automatic request');
      ai.click(); await flush(); assert.equal(postCount, 2, 'A deliberate retry remains available');
    }
    return;
  }
  if (scenario.startsWith('photo_')) {
    const link = get('review-photo-link');
    const first = link.firstElementChild;
    const next = get('review-photo-next');
    const previous = get('review-photo-previous');
    const select = checked => { photoSelect.checked = checked; form.listeners.input({target: photoSelect}); photoSelect.listeners.change.call(photoSelect); };
    const selectedStatus = get('review-photo-selection-status');
    const save = async id => {
      submit.click(); await flush();
      const sent = JSON.parse(calls.find(call => call.url.endsWith('/decision')).body);
      assert.equal(sent.photo_asset_id, id);
    };
    if (scenario === 'photo_single') {
      assert.equal(next.hidden, true);
      assert.equal(photoSelect.checked, true);
      assert.equal(submit.disabled, false);
      await save(1); return;
    }
    assert.equal(next.hidden, false);
    assert.equal(get('review-photo-position').textContent, ['photo_existing', 'photo_readonly'].includes(scenario) ? '1 / 3' : '1 / 2');
    if (scenario === 'photo_readonly') {
      assert.equal(photoSelect.checked, true);
      assert.equal(photoSelect.disabled, true);
      next.click(); assert.equal(photoSelect.checked, false);
      select(true); previous.click();
      assert.equal(photoSelect.checked, true, 'Read-only browsing must preserve the current photo');
      assert.equal(postCount, 0); return;
    }
    if (scenario === 'photo_existing') {
      assert.equal(photoSelect.checked, true);
      next.click(); assert.equal(photoSelect.checked, false);
      assert.match(selectedStatus.textContent, /1枚目/);
      assert.equal(submit.disabled, false);
      await save(null); return;
    }
    assert.equal(photoSelect.checked, false);
    assert.equal(submit.disabled, true, 'Multiple photos require one explicit selection');
    if (scenario === 'photo_preview_only') {
      next.click(); assert.equal(photoSelect.checked, false);
      assert.equal(submit.disabled, true); submit.click();
      assert.equal(postCount, 0, 'Browsing must never select or save a photo'); return;
    }
    if (scenario === 'photo_browse_selected' || scenario === 'photo_preview_error') {
      select(true); assert.equal(submit.disabled, false);
      next.click(); assert.equal(photoSelect.checked, false);
      assert.match(selectedStatus.textContent, /1枚目/);
      if (scenario === 'photo_preview_error') link.firstElementChild.onerror();
      assert.equal(submit.disabled, false, 'An unrelated preview cannot block the selected photo');
      await save(1); return;
    }
    if (scenario === 'photo_busy') {
      select(true);
      form.elements.branch_name.value = ''; form.listeners.input({target: form.elements.shop_name});
      ai.click(); await flush();
      assert.equal(photoSelect.disabled, true);
      select(false); assert.equal(photoSelect.checked, false);
      releaseAi(); await flush();
      assert.equal(photoSelect.checked, true, 'Busy input cannot change the saved selection');
      assert.equal(photoSelect.disabled, false);
      assert.equal(form.elements.shop_name.value, 'Original 123');
      assert.equal(form.elements.branch_name.value, 'New branch');
      assert.equal(postCount, 1); return;
    }
    if (scenario === 'photo_selected_error') {
      select(true); first.onerror(); assert.equal(submit.disabled, true);
      next.click(); assert.equal(submit.disabled, true, 'Browsing away cannot hide a failed selection');
      select(true); assert.equal(submit.disabled, false);
      previous.click(); assert.equal(photoSelect.checked, false);
      assert.equal(photoSelect.disabled, true);
      assert.equal(submit.disabled, false);
      await save(2); return;
    }
    if (scenario === 'photo_error' || scenario === 'photo_retry') {
      select(true); first.onerror(); assert.equal(submit.disabled, true);
      assert.equal(get('review-photo-error').hidden, false);
      if (scenario === 'photo_retry') {
        get('review-photo-retry').click();
        assert.notEqual(link.firstElementChild, first);
        link.firstElementChild.onload(); first.onerror();
        assert.equal(get('review-photo-error').hidden, true, 'A replaced image cannot report a stale error');
        assert.equal(photoSelect.checked, true);
        assert.equal(submit.disabled, false); return;
      }
    }
    next.click(); assert.equal(photoSelect.checked, false);
    select(true); assert.equal(submit.disabled, false);
    if (scenario === 'photo_uncheck') {
      select(false); assert.equal(submit.disabled, true);
      get('review-queue').children[1].click(); get('review-queue').children[0].click();
      assert.equal(photoSelect.checked, false, 'Returning must not automatically select a cleared photo');
      assert.equal(submit.disabled, true); assert.equal(postCount, 0); return;
    }
    if (scenario === 'photo_scope') {
      previous.click();
      metadataScope.click(); await flush(); identityScope.click(); await flush();
      assert.equal(get('review-photo-position').textContent, '1 / 2');
      assert.equal(photoSelect.checked, false);
      assert.match(selectedStatus.textContent, /2枚目/);
      await save(2); return;
    }
    if (scenario === 'photo_save_failure') {
      previous.click(); submit.click(); await flush();
      assert.equal(photoSelect.checked, false);
      assert.match(selectedStatus.textContent, /2枚目/);
      assert.equal(get('review-error').hidden, false);
      retry.click(); await flush();
      const posts = calls.filter(call => call.url.endsWith('/decision'));
      assert.equal(posts.length, 2);
      assert.equal(JSON.parse(posts[0].body).photo_asset_id, 2);
      assert.deepEqual(posts[0], posts[1]); return;
    }
    if (scenario === 'photo_drafts') {
      get('review-queue').children[1].click(); get('review-queue').children[0].click();
      assert.equal(photoSelect.checked, true);
    }
    assert.equal(get('review-photo-position').textContent, '2 / 2');
    assert.equal(link.firstElementChild.src, '/api/admin/evidence/photo/2');
    if (scenario === 'photo_error') first.onerror();
    assert.equal(get('review-photo-error').hidden, true);
    assert.equal(submit.disabled, false);
    await save(2); return;
  }
  if (scenario === 'readonly_navigation') {
    assert.equal(submit.disabled, true);
    assert.equal(ai.disabled, true);
    assert.equal(reject.disabled, true);
    assert.equal(get('review-queue').children[1].disabled, false);
    get('review-queue').children[1].click(); await flush();
    assert.equal(form.elements.shop_name.value, 'Original 124');
    assert.equal(form.elements.shop_name.disabled, true);
    assert.equal(filter.elements.q.disabled, false);
    submit.click(); ai.click(); reject.click(); key('a'); key('x');
    await flush(); assert.equal(postCount, 0);
    return;
  }
  if (scenario.startsWith('ai_')) {
    form.elements.shop_name.value = 'My edited name'; form.elements.phone.value = '';
    form.elements.area.value = ''; form.elements.category.value = ''; form.listeners.input({target: form.elements.shop_name});
    get('review-supplement').value = 'Near the station'; get('review-supplement').listeners.input();
    get('review-reference-url').value = 'https://example.com/reference'; get('review-reference-url').listeners.input();
    get('review-ai-search').listeners.click(); await flush();
    assert.equal(postCount, 1);
    assert.equal(form.elements.shop_name.value, 'My edited name');
    assert.equal(form.elements.phone.value, '');
    if (scenario === 'ai_conflict') {
      assert.equal(retry.textContent, '最新情報を読み込む');
      retry.listeners.click(); await flush();
      assert.equal(postCount, 1);
      assert.equal(getCount, 2);
      assert.equal(form.elements.shop_name.value, 'My edited name');
      assert.equal(get('review-supplement').value, 'Near the station');
      return;
    }
    assert.equal(form.elements.shop_name.value, 'My edited name');
    assert.equal(form.elements.branch_name.value, 'Original branch');
    assert.equal(form.elements.phone.value, '', 'Hidden fields are not AI targets');
    assert.equal(form.elements.area.value, '神田');
    assert.equal(get('review-queue').children[0].children[1].textContent, '神田', 'The queue reflects the current draft area');
    assert.equal(form.elements.category.value, '寿司');
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(submit.disabled, false);
    assert.equal(ai.disabled, true, 'No empty visible fields remain');
    ai.click(); await flush();
    assert.equal(postCount, 1, 'Completing and applying suggestions must not register data');
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    const saved = JSON.parse(calls.find(call => call.url.endsWith('/decision')).body);
    assert.equal(saved.shop.shop_name, 'My edited name');
    assert.deepEqual(saved.suggestion_evidence_urls, ['https://example.com/shop']);
    assert.equal(saved.supplement, 'Near the station');
    return;
  }
  if (scenario === 'all_remaining') {
    submit.click(); await flush();
    assert.equal(postCount, 1);
    assert.equal(get('review-queue').children.length, 2, 'Queue reload uses the server response after registration');
    assert.equal(form.elements.shop_name.readOnly, false, 'The default combined review keeps identity fields editable');
    assert.equal(new URLSearchParams(calls.at(-1).url.split('?')[1]).get('scope'), 'all');
    return;
  }
  if (['reject_cancel', 'reject_unlinked_draft'].includes(scenario)) {
    form.elements.shop_name.value = 'Retained unsaved name'; form.listeners.input({target: form.elements.shop_name});
    reject.listeners.click.call(reject);
    assert.equal(confirmations.length, 1);
    assert.match(confirmations[0], /編集中の変更は保存されません/);
    if (scenario === 'reject_cancel') {
      assert.match(confirmations[0], /Original 123/);
      assert.match(confirmations[0], /元投稿・店舗情報・操作履歴は残ります/);
      assert.match(confirmations[0], /確認対象から削除/);
    } else {
      assert.doesNotMatch(confirmations[0], /店舗情報.*削除/);
    }
    assert.equal(postCount, 0);
    assert.equal(get('review-queue').children.length, 2);
    assert.equal(form.elements.shop_name.value, 'Retained unsaved name');
    return;
  }
  if (['reject_metadata', 'reject_empty'].includes(scenario)) {
    if (scenario === 'reject_metadata') { metadataScope.listeners.click(); await flush(); }
    reject.listeners.click.call(reject); key('x');
    assert.equal(confirmations.length, scenario === 'reject_metadata' ? 2 : 0);
    assert.equal(postCount, 0);
    return;
  }
  if (['reject_shortcut', 'reject_unlinked'].includes(scenario)) {
    confirmResult = true;
    key('x'); key('x');
    assert.equal(postCount, 1);
    assert.equal(confirmations.length, 1);
    await flush();
    const payload = JSON.parse(calls.find(call => call.method === 'POST').body);
    assert.equal(payload.action, 'exclude');
    assert.equal(payload.expected_version, 4);
    assert.equal(payload.shop_version, scenario === 'reject_unlinked' ? undefined : 7);
    assert.equal(get('review-notice').hidden, false);
    assert.match(get('review-notice-text').textContent, /保存しました/);
    return;
  }
  if (scenario.startsWith('supplement_')) {
    supplement.value = 'Source to check / 原文のメモ'; supplement.listeners.input();
    assert.equal(postCount, 0, 'Typing the optional supplement must not save');
    key('d');
    assert.equal(postCount, 0, 'The retired defer shortcut must not save');
    if (scenario === 'supplement_drafts') {
      get('review-queue').children[1].listeners.click();
      assert.equal(supplement.value, '');
      supplement.value = 'Second item note'; supplement.listeners.input();
      get('review-queue').children[0].listeners.click();
      assert.equal(supplement.value, 'Source to check / 原文のメモ');
      metadataScope.listeners.click(); await flush();
      assert.equal(supplement.value, '');
      supplement.value = 'Metadata note'; supplement.listeners.input();
      identityScope.listeners.click(); await flush();
      assert.equal(supplement.value, 'Source to check / 原文のメモ');
    }
    submit.click();
    assert.equal(postCount, 1, 'Only registration saves the supplement');
    const firstPost = calls.find(call => call.method === 'POST');
    const firstPayload = JSON.parse(firstPost.body);
    assert.equal(firstPayload.action, 'edit_and_approve');
    assert.equal(firstPayload.supplement, 'Source to check / 原文のメモ');
    assert.equal(firstPayload.expected_version, 4);
    if (scenario === 'supplement_retry' || scenario === 'supplement_retry_edit') {
      assert.equal(supplement.disabled, true);
      releasePost(); await flush();
      assert.equal(supplement.disabled, false);
      assert.equal(supplement.value, 'Source to check / 原文のメモ');
      if (scenario === 'supplement_retry_edit') {
        supplement.value = 'Revised note after failed save'; supplement.listeners.input();
        retry.listeners.click(); await flush();
      } else { retry.listeners.click(); await flush(); }
      const posts = calls.filter(call => call.method === 'POST');
      assert.equal(posts.length, 2);
      if (scenario === 'supplement_retry_edit') {
        assert.equal(JSON.parse(posts[1].body).supplement, 'Revised note after failed save');
        assert.equal(JSON.parse(posts[1].body).expected_version, 4);
      } else assert.deepEqual(posts[1], firstPost);
    } else await flush();
    assert.equal(confirmations.length, 0);
    assert.equal(get('review-queue').children.length, 1);
    assert.equal(supplement.value, scenario === 'supplement_drafts' ? 'Second item note' : '');
    assert.match(get('review-notice-text').textContent, /保存しました/);
    return;
  }
  if (scenario.startsWith('link_')) {
    const previewLink = () => link.listeners.submit({ preventDefault() {}, currentTarget: link });
    const confirmLink = () => { linkConfirm.checked = true; linkConfirm.listeners.change(); };
    const applyLink = () => linkApply.listeners.click();
    link.elements.target_shop_id.value = '900';
    link.elements.note.value = 'Official address matches the original post';
    link.elements.note.listeners.input();
    if (scenario === 'link_source_draft') {
      get('review-queue').children[1].listeners.click();
      form.elements.phone.value = 'Retained draft phone'; form.listeners.input({target: form.elements.shop_name});
      get('review-queue').children[0].listeners.click();
      link.elements.target_shop_id.value = '900'; link.elements.note.value = 'Source relationship evidence';
    }
    if (scenario === 'link_dirty' || scenario === 'link_other_scope_draft') {
      if (scenario === 'link_other_scope_draft') { metadataScope.listeners.click(); await flush(); }
      form.elements.address.value = 'Unsaved address'; form.listeners.input({target: form.elements.shop_name});
      if (scenario === 'link_other_scope_draft') {
        identityScope.listeners.click(); await flush();
        link.elements.target_shop_id.value = '900'; link.elements.note.value = 'Evidence';
      }
      previewLink(); await flush();
      assert.equal(linkPreviewCount, 0);
      assert.equal(postCount, 0);
      assert.match(get('review-error-text').textContent, /入力は保持/);
      if (scenario === 'link_other_scope_draft') { metadataScope.listeners.click(); await flush(); }
      assert.equal(form.elements.address.value, 'Unsaved address');
      return;
    }
    if (scenario === 'link_linked') {
      link.elements.note.value = '   '; previewLink(); await flush();
      link.elements.note.value = 'x'.repeat(2001); previewLink(); await flush();
      assert.equal(linkPreviewCount, 0);
      link.elements.note.value = 'Original post and official address';
    }
    previewLink(); await flush();
    if (['link_newer_mention', 'link_newer_source', 'link_changed_source'].includes(scenario)) {
      assert.equal(get('review-link-preview').hidden, true);
      assert.equal(linkApply.disabled, true);
      assert.match(get('review-error-text').textContent, /最新情報を読み込んで/);
      const originalNote = link.elements.note.value;
      confirmLink(); applyLink(); await flush(); assert.equal(postCount, 0);
      if (scenario === 'link_newer_mention') items[0].version = 5;
      if (scenario === 'link_newer_source') items[0].shop.version = 8;
      if (scenario === 'link_changed_source') items[0].shop.id = 999;
      retry.listeners.click(); await flush();
      assert.equal(link.elements.note.value, originalNote);
      assert.equal(link.elements.target_shop_id.value, '900');
      assert.equal(get('review-link-preview').hidden, true);
      previewLink(); await flush();
      assert.equal(get('review-link-preview').hidden, false);
      assert.equal(postCount, 0, 'A refresh and a new preview never write automatically');
      return;
    }
    if (scenario.startsWith('link_preview_')) {
      assert.equal(linkPreview.disabled, true);
      if (scenario === 'link_preview_target') {
        link.elements.target_shop_id.value = '901'; link.elements.target_shop_id.listeners.input();
        previewLink(); await flush();
        assert.match(get('review-link-target-preview').children[0].textContent, /ID 901/);
      } else if (scenario === 'link_preview_selection') {
        get('review-queue').children[1].listeners.click();
      } else if (scenario === 'link_preview_filter') {
        filter.elements.q.value = 'Another filter'; filter.listeners.input();
      } else {
        metadataScope.listeners.click(); await flush();
        assert.equal(get('review-link-section').hidden, true);
      }
      releasePreview(); await flush();
      if (scenario === 'link_preview_target') {
        assert.match(get('review-link-target-preview').children[0].textContent, /ID 901/);
      } else {
        assert.equal(get('review-link-preview').hidden, true);
        assert.equal(linkApply.disabled, true);
      }
      assert.equal(postCount, 0);
      return;
    }
    if (scenario === 'link_bad_mention' || scenario === 'link_bad_target') {
      assert.equal(get('review-link-preview').hidden, true);
      confirmLink(); applyLink(); await flush();
      assert.equal(postCount, 0);
      assert.match(get('review-error-text').textContent, /一致しません/);
      return;
    }
    assert.equal(get('review-link-preview').hidden, false);
    assert.equal(linkApply.disabled, true);
    assert.match(get('review-link-counts').textContent, /変更する確認項目: 1件/);
    assert.match(get('review-link-target-preview').children[0].textContent, /Target shop.*本店.*神田/);
    assert.match(get('review-link-target-preview').children[1].textContent, /Keep this memo/);
    assert.match(get('review-link-status').textContent, /あとで確認（変更しません）/);
    assert.equal(get('review-link-empty-warning').hidden, scenario === 'link_unlinked');
    applyLink(); await flush(); assert.equal(postCount, 0, 'Confirmation is required');
    confirmLink();
    if (scenario === 'link_note_change') {
      link.elements.note.value = 'Changed evidence'; link.elements.note.listeners.input();
      applyLink(); await flush();
      assert.equal(postCount, 0);
      assert.equal(linkConfirm.checked, false);
      assert.equal(get('review-link-preview').hidden, true);
      return;
    }
    applyLink(); applyLink(); await flush();
    assert.equal(postCount, 1, 'Double-click cannot send twice');
    const sent = JSON.parse(calls.find(call => call.method === 'POST').body);
    assert.equal(sent.expected_version, 4);
    assert.equal(sent.source_shop_id, scenario === 'link_unlinked' ? null : 223);
    assert.equal(sent.source_shop_version, scenario === 'link_unlinked' ? null : 7);
    assert.equal(sent.target_shop_id, 900);
    assert.equal(sent.target_shop_version, 11);
    assert.deepEqual(Object.keys(sent).sort(), ['expected_version', 'note', 'source_shop_id', 'source_shop_version', 'target_shop_id', 'target_shop_version']);
    if (scenario === 'link_network') {
      targetShop.version = 12;
      retry.listeners.click(); await flush();
      assert.equal(postCount, 2);
      const posts = calls.filter(call => call.method === 'POST');
      assert.equal(posts[0].body, posts[1].body);
      assert.equal(posts[0].url, posts[1].url);
    }
    if (scenario === 'link_network' || scenario === 'link_conflict') {
      assert.equal(get('review-link-preview').hidden, true);
      assert.equal(linkConfirm.checked, false);
      const beforeRefresh = postCount;
      confirmLink(); applyLink(); await flush();
      assert.equal(postCount, beforeRefresh, 'A rejected preview cannot be applied again');
      retry.listeners.click(); await flush();
      assert.equal(postCount, beforeRefresh, 'Conflict recovery only reloads');
      assert.equal(linkPreviewCount, 1, 'A new preview requires an explicit request');
      assert.equal(get('review-link-preview').hidden, true);
      return;
    }
    assert.equal(link.elements.target_shop_id.disabled, true);
    releasePost(); await flush();
    assert.equal(postCount, 1);
    assert.equal(get('review-queue').children.length, 1);
    if (scenario === 'link_source_sync' || scenario === 'link_source_draft') {
      if (scenario === 'link_source_draft') assert.equal(form.elements.phone.value, 'Retained draft phone');
      form.elements.category.value = 'Updated category'; form.listeners.input({target: form.elements.shop_name});
      form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
      const nextPost = JSON.parse(calls.filter(call => call.method === 'POST')[1].body);
      assert.equal(nextPost.shop_version, 8, 'Remaining source mentions use the new source shop version');
      if (scenario === 'link_source_draft') assert.equal(nextPost.shop.phone, 'Retained draft phone');
    }
    return;
  }
  if (scenario === 'event_reason') {
    assert.match(get('review-difference').textContent, /催事会場は登録対象外/);
    assert.match(get('review-difference').textContent, /出店元の常設店舗/);
    assert.doesNotMatch(get('review-difference').textContent, /event_excluded/);
    assert.equal(get('review-extraction-error').textContent, '出店元の支店を特定できません。');
    const candidate = get('review-candidates').children[0].children.find(child => child.tagName === 'BUTTON');
    assert.equal(submit.disabled, true);
    assert.equal(candidate.disabled, true);
    assert.equal(reject.disabled, false, 'Excluding the review item remains available');
    assert.equal(submit.disabled, true);
    submit.click();
    candidate.listeners.click();
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    assert.equal(postCount, 0);
    form.elements.shop_name.value = 'Verified permanent shop'; form.listeners.input({target: form.elements.shop_name});
    assert.equal(submit.disabled, false);
    assert.equal(candidate.disabled, true);
    return;
  }
  if (scenario === 'response_lost') {
    form.elements.address.value = 'Saved once'; form.listeners.input({target: form.elements.shop_name});
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    assert.equal(postCount, 1);
    assert.equal(form.elements.address.value, 'Saved once');
    items[0].shop = { ...items[0].shop, version: 9, phone: 'Concurrent phone' };
    retry.listeners.click(); await flush();
    assert.equal(postCount, 2);
    const posts = calls.filter(call => call.method === 'POST');
    assert.equal(posts[0].body, posts[1].body, 'A retry must keep the original version guard');
    retry.listeners.click(); await flush();
    assert.equal(postCount, 2, 'Conflict recovery must only read; never reapply a committed request');
    assert.equal(form.elements.address.value, 'Saved once');
    assert.equal(form.elements.phone.value, 'Concurrent phone');
    assert.equal(get('review-edit-conflicts').hidden, true);
    assert.equal(get('review-error').hidden, true);
    assert.match(get('review-notice-text').textContent, /確認済み/);
    return;
  }
  if (scenario.startsWith('shared_shop')) {
    if (scenario === 'shared_shop_draft') {
      get('review-queue').children[1].listeners.click();
      form.elements.phone.value = '03-1234-5678'; form.listeners.input({target: form.elements.shop_name});
      get('review-queue').children[0].listeners.click();
    }
    form.elements.address.value = 'Corrected shared address'; form.listeners.input({target: form.elements.shop_name});
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    assert.equal(form.elements.address.value, 'Corrected shared address', 'The next mention must show the saved shared shop');
    if (scenario === 'shared_shop_draft') {
      assert.equal(form.elements.phone.value, '03-1234-5678');
    }
    form.elements.category.value = 'Updated category'; form.listeners.input({target: form.elements.shop_name});
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    const second = JSON.parse(calls.filter(call => call.method === 'POST')[1].body);
    assert.equal(second.shop_version, 8, 'The next mention must use the new shared shop version');
    assert.equal(second.expected_version, 4, 'The next mention retains its own version');
    assert.equal(second.shop.address, 'Corrected shared address');
    if (scenario === 'shared_shop_draft') assert.equal(second.shop.phone, '03-1234-5678');
    return;
  }
  if (scenario.startsWith('overlap_')) {
    form.elements.address.value = 'My address'; form.listeners.input({target: form.elements.shop_name});
    items[0] = { ...items[0], version: 5, shop: { ...items[0].shop, version: 8, address: 'Their address', phone: 'Their phone' } };
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    retry.listeners.click(); await flush();
    assert.equal(form.elements.address.value, 'My address');
    assert.equal(form.elements.phone.value, 'Their phone');
    assert.equal(get('review-edit-conflicts').hidden, false);
    assert.equal(submit.disabled, true);
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    assert.equal(postCount, 1, 'Overlapping changes need an explicit field choice');
    const row = get('review-edit-conflicts').children[1];
    assert.match(row.children[0].textContent, /My address.*Their address/);
    row.children[scenario === 'overlap_latest' ? 2 : 1].listeners.click();
    assert.equal(submit.disabled, false);
    assert.equal(get('review-edit-conflicts').hidden, true);
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    const saved = JSON.parse(calls.filter(call => call.method === 'POST')[1].body);
    assert.equal(saved.shop_version, 8);
    assert.equal(saved.expected_version, 5);
    assert.equal(saved.shop.phone, 'Their phone');
    assert.equal(saved.shop.address, scenario === 'overlap_latest' ? 'Their address' : 'My address');
    if (scenario === 'overlap_repeat') {
      items[0] = { ...items[0], version: 6, shop: { ...items[0].shop, version: 9, address: 'Their newer address', phone: 'Their newer phone' } };
      retry.listeners.click(); await flush();
      assert.equal(form.elements.address.value, 'My address');
      assert.equal(form.elements.phone.value, 'Their newer phone');
      assert.equal(submit.disabled, true);
      get('review-edit-conflicts').children[1].children[1].listeners.click();
      form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
      const retried = JSON.parse(calls.filter(call => call.method === 'POST')[2].body);
      assert.equal(retried.expected_version, 6);
      assert.equal(retried.shop_version, 9);
      assert.equal(retried.shop.address, 'My address');
      assert.equal(retried.shop.phone, 'Their newer phone');
    }
    return;
  }
  if (['collision', 'validation', 'missing_shop', 'merge_target', 'event_origin_error'].includes(scenario)) {
    form.elements.shop_name.value = 'Unsaved name'; form.listeners.input({target: form.elements.shop_name});
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    assert.equal(form.elements.shop_name.value, 'Unsaved name');
    assert.equal(retry.hidden, true, 'A business conflict or invalid field cannot be repaired by replaying the request');
    const expectedMessage = {
      collision: /shop_id=999/, validation: /エリア.*候補: 神田/,
      missing_shop: /店・支店を確認/, merge_target: /統合先のIDを入力し直して/,
      event_origin_error: /出店元の常設店舗を根拠付きで確認/,
    };
    assert.match(get('review-error-text').textContent, expectedMessage[scenario]);
    assert.equal(getCount, 1);
    return;
  }
  if (scenario === 'reject') {
    confirmResult = true;
    form.elements.shop_name.value = 'Unsaved draft'; form.listeners.input({target: form.elements.shop_name});
    reject.listeners.click.call(reject);
    assert.equal(postCount, 1, 'Reject must send the confirmed decision once');
    assert.equal(confirmations.length, 1);
    assert.equal(reject.disabled, true);
    assert.equal(reject.getAttribute('aria-busy'), 'true');
    reject.listeners.click.call(reject);
    assert.equal(postCount, 1, 'Repeated clicks must not send another decision');
    const firstPost = calls.find(call => call.method === 'POST');
    assert.equal(firstPost.url, '/api/admin/reviews/123/decision');
    assert.deepEqual(JSON.parse(firstPost.body), {
      action: 'exclude', expected_version: 4, scope: 'identity', shop_version: 7,
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
    assert.equal(confirmations.length, 1, 'Retry must not repeat the already accepted confirmation');
    assert.equal(get('review-error').hidden, true);
    assert.equal(get('review-queue').children.length, 1);
    assert.equal(form.elements.shop_name.value, 'Original 124');
    assert.equal(document.activeElement, get('review-queue').children[0]);
    return;
  }
  if (scenario.startsWith('draft_')) {
    form.elements.shop_name.value = 'Draft shop name'; form.listeners.input({target: form.elements.shop_name});
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
    form.elements.phone.value = '03-1111-2222'; form.listeners.input({target: form.elements.shop_name});
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
    retry.listeners.click(); await flush();
    if (scenario === 'draft_reload_conflict') {
      assert.equal(form.elements.shop_name.value, 'Draft shop name');
      assert.equal(form.elements.phone.value, '03-1111-2222');
      assert.equal(postCount, 1);
      retry.listeners.click(); await flush();
    }
    assert.equal(confirmations.length, 0, 'Refresh must preserve inputs without a discard dialog');
    assert.equal(postCount, 1, 'Resolving a conflict must only fetch the latest values');
    assert.equal(form.elements.shop_name.value, 'Draft shop name');
    assert.equal(form.elements.phone.value, '03-1111-2222');
    assert.equal(form.elements.address.value, 'Saved in another tab');
    assert.equal(get('review-error').hidden, true);
    assert.equal(submit.disabled, false);
    assert.equal(calls.at(-1).url, '/api/admin/reviews/123', 'Recovery must find the item outside filters and pagination');
    form.elements.shop_name.value = 'Reapplied name'; form.listeners.input({target: form.elements.shop_name});
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
    submit.click(); await flush();
    assert.equal(postCount, 1);
    assert.equal(get('review-queue').children.length, 0);
    assert.equal(get('review-decision').hidden, true);
    assert.equal(get('review-result-count').textContent, '0');
    assert.equal(document.activeElement, filter.elements.q, 'Empty results focus the visible search field');
    assert.match(get('review-notice-text').textContent, /保存しました/);
    return;
  }
  if (['scope_switch', 'scope_failure'].includes(scenario)) {
    form.elements.address.value = 'Saved identity draft'; form.listeners.input({target: form.elements.shop_name});
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
    assert.equal(get('review-reject').hidden, false);
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
    assert.match(button.textContent, /入力欄で確認/);
    form.elements.shop_name.value = 'Edited draft'; form.listeners.input({target: form.elements.shop_name});
    assert.equal(button.disabled, false);
    button.listeners.click(); await flush();
    assert.equal(postCount, 0);
    assert.equal(form.elements.shop_name.value, 'Edited draft', 'Even a selected candidate cannot overwrite a filled field');
    assert.equal(form.elements.area.value, '銀座');
    form.elements.shop_name.value = ''; form.listeners.input({target: form.elements.shop_name});
    button.listeners.click(); await flush();
    assert.equal(form.elements.shop_name.value, 'Candidate shop', 'A selected candidate fills an empty field');
    assert.equal(form.elements.area.value, '銀座');
    assert.equal(postCount, 0, 'A candidate must not register until the user saves');
    return;
  }
  if (scenario === 'metadata_branch') {
    metadataScope.listeners.click(); await flush();
    assert.equal(form.elements.branch_name.value, '');
    assert.equal(form.elements.branch_name.readOnly, true);
    form.elements.area.value = '新宿'; form.listeners.input({target: form.elements.shop_name});
    form.listeners.submit({ preventDefault() {}, currentTarget: form }); await flush();
    const payload = JSON.parse(calls.find(call => call.method === 'POST').body);
    assert.equal(payload.scope, 'metadata');
    assert.equal(payload.shop.branch_name, null, 'Do not send an extracted branch as a metadata edit');
    assert.equal(payload.shop.area, '新宿');
    return;
  }
  if (scenario === 'reasons_and_counts') {
    assert.equal(get('review-result-count').textContent, '2');
    assert.equal(get('count-unresolved').textContent, '2');
    assert.equal(get('review-queue').children[0].children[1].textContent, '銀座', 'Queue metadata contains only the area');
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
    assert.equal(get('count-unresolved').textContent, '2');
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
    assert.equal(document.activeElement, filter.elements.q, 'Empty results focus the visible search field');
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
    assert.equal(submit.disabled, true);
    filter.listeners.submit({ preventDefault() {} });
    await flush();
    assert.equal(submit.disabled, true);
    submit.click(); key('a');
    await flush();
    assert.equal(postCount, 0);
    return;
  }
  assert.equal(submit.disabled, false);
  if (scenario === 'dirty') {
    form.elements.shop_name.value = 'Edited draft'; form.listeners.input({target: form.elements.shop_name});
    assert.equal(submit.disabled, false, 'A valid edited draft can be registered');
    assert.equal(postCount, 0);
    get('review-queue').children[1].listeners.click();
    assert.equal(submit.disabled, false);
    get('review-queue').children[0].listeners.click();
    assert.equal(form.elements.shop_name.value, 'Edited draft');
    filter.listeners.submit({ preventDefault() {} }); await flush();
    assert.equal(form.elements.shop_name.value, 'Edited draft');
    assert.equal(submit.disabled, false);
    key('a'); await flush();
    assert.equal(postCount, 1);
    const payload = JSON.parse(calls.find(call => call.method === 'POST').body);
    assert.equal(payload.action, 'edit_and_approve');
    assert.equal(payload.shop.shop_name, 'Edited draft');
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
    form.elements.area.value = '新宿'; form.listeners.input({target: form.elements.shop_name});
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
  form.listeners.input({target: form.elements.shop_name});
  if (scenario === 'reload') {
    get('review-filter').listeners.submit({ preventDefault() {} });
    await flush();
    Object.entries(edit).forEach(([key, value]) => assert.equal(form.elements[key].value, value));
    assert.equal(postCount, 0);
    assert.equal(submit.disabled, false);
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
    assert.equal(submit.disabled, true);
    assert.equal(retry.disabled, true);
    assert.equal(get('review-queue').children[0].disabled, true);
    form.listeners.submit({ preventDefault() {}, currentTarget: form });
    submit.click(); filter.listeners.submit({ preventDefault() {} }); key('a');
    assert.equal(postCount, 1);
    releasePost(); await flush();
    assert.equal(form.elements.shop_name.disabled, false);
    assert.equal(filter.elements.scope.disabled, false);
    assert.equal(submit.textContent, '登録');
    assert.equal(submit.getAttribute('aria-busy'), undefined);
    assert.equal(retry.disabled, false);
    assert.equal(submit.disabled, false);
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
    assert.equal(submit.disabled, false);
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
    assert.equal(submit.disabled, false);
    assert.equal(get('review-error').focusCount, 1);
    assert.equal(get('review-error').scrollCount, 1);
    if (scenario === 'conflict') {
      assert.match(get('review-error-text').textContent, /別の更新/);
      assert.equal(retry.textContent, '最新情報を読み込む');
      retry.listeners.click(); await flush();
      assert.equal(getCount, 2);
      assert.equal(postCount, 1);
      assert.equal(form.elements.shop_name.value, 'Edited name');
      assert.equal(form.elements.address.value, 'Edited address');
      assert.equal(get('review-error').hidden, true);
      assert.equal(confirmations.length, 0);
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
    harness_path = tmp_path / "review_harness.cjs"
    harness_path.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(harness_path), scenario], cwd=PROJECT_ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
