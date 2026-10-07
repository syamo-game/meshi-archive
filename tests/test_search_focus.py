from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", [
    "search", "search_second_load", "pagination", "modified_pagination",
    "plain_load", "filter_hash", "clear_filters", "legacy_navigation",
])
def test_full_page_search_and_pagination_restore_the_correct_focus(
    scenario: str, tmp_path: Path,
) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to exercise search focus."
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[2];
const script = fs.readFileSync('web/static/js/main.js', 'utf8');
const storage = new Map();
class Control {
  constructor(value = '') {
    this.value = value; this.defaultValue = value; this.focusCount = 0;
    this.listeners = new Map(); this.attributes = {}; this.textContent = '';
  }
  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback); this.listeners.set(type, callbacks);
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { this.focusCount += 1; }
}
function loadPage(hash = '') {
  const query = new Control('銀座 鮨');
  const form = new Control(); form.reset = () => { query.value = query.defaultValue; };
  const heading = new Control(), status = new Control(), results = new Control();
  const elements = new Map([
    ['q-input', query], ['filter-form', form], ['result-heading', heading],
    ['result-status', status], ['search-results', results],
  ]);
  const pageEvents = new Map(), documentEvents = new Map();
  const addListener = (events, type, callback) => {
    const callbacks = events.get(type) || [];
    callbacks.push(callback); events.set(type, callbacks);
  };
  const document = {
    getElementById: id => elements.get(id) || null,
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener(type, callback) { addListener(documentEvents, type, callback); },
  };
  const window = {
    location: { hash, search: '?q=銀座%20鮨' },
    sessionStorage: {
      getItem: key => storage.get(key) || null,
      setItem(key, value) { storage.set(key, value); },
      removeItem(key) { storage.delete(key); },
    },
    requestAnimationFrame: callback => callback(),
    addEventListener(type, callback) { addListener(pageEvents, type, callback); },
  };
  vm.runInNewContext(script, {
    document, window, console, URL, URLSearchParams,
    fetch() { assert.fail('Focus restoration must not make an API request'); },
  });
  for (const callback of pageEvents.get('pageshow') || []) callback({ persisted: false });
  return { query, form, heading, status, results, documentEvents };
}
const first = loadPage();
assert.equal(first.query.focusCount, 0, 'An ordinary initial load does not steal focus');
assert.equal(first.heading.focusCount, 0);
if (scenario.startsWith('search')) {
  for (const callback of first.form.listeners.get('submit') || []) callback();
  assert.equal(first.results.attributes['aria-busy'], 'true');
  const loaded = loadPage();
  assert.equal(loaded.query.focusCount, 1, 'Submitting search returns focus to the keyword input');
  assert.equal(loaded.heading.focusCount, 0, 'Submitting search does not focus the saved-shops heading');
  assert.equal(loaded.query.value, '銀座 鮨', 'The submitted query remains editable');
  assert.equal(loaded.status.textContent, '検索結果を更新しました。');
  assert.equal(storage.size, 0, 'The focus request is consumed once');
  if (scenario === 'search_second_load') {
    const next = loadPage();
    assert.equal(next.query.focusCount, 0, 'Unrelated reloads do not replay the search focus request');
    assert.equal(next.heading.focusCount, 0);
  }
} else if (['pagination', 'modified_pagination', 'clear_filters'].includes(scenario)) {
  const link = new Control();
  const event = {
    target: { closest: selector => selector.includes('[data-result-navigation]') ? link : null },
    button: 0, defaultPrevented: false, ctrlKey: scenario === 'modified_pagination',
  };
  for (const callback of first.documentEvents.get('click') || []) callback(event);
  const loaded = loadPage(scenario === 'clear_filters' ? '#q-input' : '');
  assert.equal(loaded.heading.focusCount, scenario === 'pagination' ? 1 : 0);
  assert.equal(loaded.query.focusCount, scenario === 'clear_filters' ? 1 : 0);
  assert.equal(loaded.query.value, '銀座 鮨');
  assert.equal(storage.size, 0);
} else if (scenario === 'filter_hash') {
  const loaded = loadPage('#q-input');
  assert.equal(loaded.query.focusCount, 1);
  assert.equal(loaded.heading.focusCount, 0);
} else if (scenario === 'legacy_navigation') {
  storage.set('meshi-archive:focus-search-results', 'true');
  const loaded = loadPage();
  assert.equal(loaded.heading.focusCount, 1, 'Existing pagination intents remain compatible');
  assert.equal(loaded.query.focusCount, 0);
  assert.equal(storage.size, 0);
} else {
  const loaded = loadPage();
  assert.equal(loaded.query.focusCount, 0);
  assert.equal(loaded.heading.focusCount, 0);
}
"""
    script_path = tmp_path / "search-focus.cjs"
    script_path.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(script_path), scenario], cwd=PROJECT_ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
