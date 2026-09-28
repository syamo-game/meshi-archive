from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["network", "server", "auth", "expired", "validation", "photo", "html", "preview", "absent"])
def test_shop_edit_retains_file_and_fields_and_retries_in_place(scenario: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required to exercise the shop editor."
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const scenario = process.argv[1];
class Element {
  constructor(id) {
    this.id = id; this.attributes = {}; this.listeners = {}; this.value = '';
    this.hidden = false; this.disabled = false; this.readOnly = false; this.name = '';
    this.textContent = ''; this.focusCount = 0; this.classes = new Set();
    this.classList = { toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name) };
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  focus() { assert.equal(this.disabled, false); this.focusCount += 1; }
  scrollIntoView() {}
}
class Input extends Element {}
class Select extends Element {}
class Textarea extends Element {}
class Button extends Element {}
class Image extends Element {
  get src() { return this.getAttribute('src'); }
  set src(value) { this.setAttribute('src', value); }
}
class Form extends Element {
  constructor(id) { super(id); this.elements = []; this.action = 'https://meshi.test/shop/12/edit'; }
  reset() { throw new Error('Resetting the editor would discard the selected File'); }
}
const elements = new Map();
const add = (element) => { elements.set(element.id, element); return element; };
const form = add(new Form('shop-edit-form'));
const submit = add(new Button('shop-edit-submit'));
submit.textContent = '保存する';
const preview = add(new Image('shop-photo-preview'));
preview.setAttribute('data-current-src', '/static/shop-images/current.jpg');
preview.src = '/static/shop-images/current.jpg';
const empty = add(new Element('shop-photo-empty')); empty.hidden = true;
const label = add(new Element('shop-photo-preview-label')); label.textContent = '現在の写真';
const error = add(new Element('shop-edit-error')); error.hidden = true;
const controls = {};
const values = {
  csrf_token: 'test-csrf', return_to: '/?q=カフェ&area=銀座', shop_name: '入力した店名',
  area: '銀座', category: 'カフェ', url: 'https://example.com/edited', rating: '4',
  visited_at: '2026-09-20', memo: 'メモ1行目\n2行目', address: '入力した住所', phone: '03-1234-5678',
};
const ids = { shop_name: 'shop-name', area: 'shop-area', url: 'shop-url', rating: 'shop-rating', visited_at: 'shop-visited-at' };
for (const [name, value] of Object.entries(values)) {
  const Constructor = ['area', 'category', 'rating'].includes(name) ? Select : name === 'memo' ? Textarea : Input;
  const control = add(new Constructor(ids[name] || 'shop-' + name));
  control.name = name; control.value = value; controls[name] = control;
  form.elements.push(control);
}
controls.phone.readOnly = true;
const originalDisabled = add(new Select('shop-area-prefecture'));
originalDisabled.disabled = true; form.elements.push(originalDisabled);
const photo = add(new Input('shop-photo')); photo.name = 'photo'; photo.type = 'file'; photo.files = [];
const visited = add(new Input('shop-is-visited')); visited.name = 'is_visited'; visited.type = 'checkbox'; visited.checked = true; visited.value = 'on';
form.elements.push(photo, visited, submit);
for (const id of [...Object.values(ids), 'shop-photo']) add(new Element(id + '-error'));
controls.area.setAttribute('aria-describedby', 'shop-area-help');
photo.setAttribute('aria-describedby', 'shop-photo-help');
const firstFile = new File([Buffer.from('selected picture bytes')], '食事.jpg', { type: 'image/jpeg' });
const secondFile = new File([Buffer.from('second picture bytes')], '食事2.png', { type: 'image/png' });
class EditorFormData extends FormData {
  constructor(source) {
    super();
    for (const control of source.elements) {
      if (!control.name || control.disabled) continue;
      if (control.type === 'checkbox' && !control.checked) continue;
      if (control.type === 'file') {
        for (const file of control.files) this.append(control.name, file);
      } else this.append(control.name, control.value);
    }
  }
}
const created = [];
const revoked = [];
class PreviewURL extends URL {
  static createObjectURL(file) { const url = URL.createObjectURL(file); created.push(url); return url; }
  static revokeObjectURL(url) { revoked.push(url); URL.revokeObjectURL(url); }
}
const events = {};
const navigations = [];
const window = {
  location: { href: 'https://meshi.test/shop/12', origin: 'https://meshi.test', assign: url => navigations.push(url) },
  addEventListener: (type, callback) => { events[type] = callback; },
};
const calls = [];
let releaseFirst;
let currentCsrf = 'test-csrf';
let authenticated = true;
const json = (status, body, headers = {}) => new Response(JSON.stringify(body), {
  status, headers: new Headers({ 'Content-Type': 'application/json', ...headers }),
});
async function fetchRequest(url, options) {
  calls.push({ url, options });
  if (calls.length === 1) await new Promise(resolve => { releaseFirst = resolve; });
  if (scenario === 'network' && calls.length === 1) throw new Error('Connection lost');
  if (scenario === 'server' && calls.length === 1) return json(503, { detail: '保存できませんでした。入力内容と写真は保持されています。' });
  if (['auth', 'expired'].includes(scenario)) {
    if (calls.length === 1) {
      currentCsrf = 'renewed-test-csrf';
      if (scenario === 'expired') authenticated = false;
    }
    if (!authenticated) return json(401, { detail: 'ログインしてください。' }, { 'x-csrf-token': currentCsrf });
    if (options.body.get('csrf_token') !== currentCsrf) return json(403, { detail: '画面の認証情報が更新されました。もう一度保存してください。' }, { 'X-CSRF-Token': currentCsrf });
  }
  if (scenario === 'html' && calls.length === 1) return new Response('<html>Login</html>', { status: 200, headers: { 'Content-Type': 'text/html' } });
  if (scenario === 'validation' && options.body.get('url').startsWith('ftp:')) {
    return json(400, { detail: '入力内容を確認してください。', errors: { url: '有効なURLを入力してください。', area: '候補にあるエリアを選んでください。' } });
  }
  if (scenario === 'photo' && options.body.get('photo') === firstFile) {
    return json(400, { detail: '入力内容を確認してください。', errors: { photo: '画像ファイルを読み取れませんでした。' } });
  }
  return json(200, { redirect_url: '/shop/12?saved=true&return_to=%2F%3Fq%3Dcafe' });
}
vm.runInNewContext(fs.readFileSync('web/static/js/shop-edit.js', 'utf8'), {
  document: { getElementById: id => scenario === 'absent' ? null : elements.get(id) || null },
  HTMLFormElement: Form, HTMLInputElement: Input, HTMLSelectElement: Select, HTMLTextAreaElement: Textarea,
  HTMLImageElement: Image, HTMLButtonElement: Button, FormData: EditorFormData, URL: PreviewURL,
  window, fetch: fetchRequest, console,
});
const event = { preventDefault() {} };
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  if (scenario === 'absent') {
    assert.equal(form.listeners.submit, undefined);
    return;
  }
  assert.equal(preview.src, '/static/shop-images/current.jpg');
  photo.files = [firstFile]; photo.listeners.change();
  assert.equal(preview.src, created[0]);
  assert.equal(label.textContent, '選択した写真（保存前）');
  assert.equal(empty.hidden, true);
  if (scenario === 'preview') {
    photo.files = [secondFile]; photo.listeners.change();
    assert.deepEqual(revoked, [created[0]]);
    assert.equal(preview.src, created[1]);
    events.pagehide({ persisted: true });
    assert.equal(revoked.length, 1);
    events.pagehide({ persisted: false });
    assert.deepEqual(revoked, created);
    photo.files = []; photo.listeners.change();
    assert.equal(preview.src, '/static/shop-images/current.jpg');
    assert.equal(label.textContent, '現在の写真');
    const huge = new File([new Uint8Array(20 * 1024 * 1024 + 1)], '大きい.jpg', { type: 'image/jpeg' });
    photo.files = [huge]; photo.listeners.change();
    assert.match(elements.get('shop-photo-error').textContent, /20MiB/);
    await form.listeners.submit(event);
    assert.equal(calls.length, 0);
    assert.equal(photo.files[0], huge);
    photo.files = [new File(['gif'], '対象外.gif', { type: 'image/gif' })]; photo.listeners.change();
    assert.match(elements.get('shop-photo-error').textContent, /JPEG・PNG・WebP/);
    assert.equal(preview.hidden, true);
    return;
  }
  if (scenario === 'validation') { controls.url.value = 'ftp://example.com'; controls.area.value = '存在しないエリア'; }
  const originalValues = Object.fromEntries(Object.entries(controls).map(([name, control]) => [name, control.value]));
  const pending = form.listeners.submit(event);
  await flush();
  assert.equal(calls.length, 1);
  assert.equal(submit.textContent, '保存中…');
  assert.equal(form.getAttribute('aria-busy'), 'true');
  assert.equal(photo.disabled, true);
  assert.equal(controls.shop_name.disabled, true);
  assert.equal(controls.phone.readOnly, true);
  assert.equal(calls[0].options.headers['X-Requested-With'], 'XMLHttpRequest');
  assert.equal(calls[0].options.headers.Accept, 'application/json');
  assert.equal(calls[0].options.headers['Content-Type'], undefined);
  assert.equal(calls[0].options.body.get('photo'), firstFile);
  assert.equal(calls[0].options.body.get('csrf_token'), 'test-csrf');
  assert.equal(calls[0].options.body.get('is_visited'), 'on');
  controls.shop_name.value = 'after snapshot';
  assert.equal(calls[0].options.body.get('shop_name'), originalValues.shop_name);
  controls.shop_name.value = originalValues.shop_name;
  await form.listeners.submit(event);
  assert.equal(calls.length, 1, 'A second submit must not send a second request while saving');
  releaseFirst(); await pending;
  assert.equal(photo.files[0], firstFile);
  for (const [name, value] of Object.entries(originalValues)) {
    const expected = name === 'csrf_token' && ['auth', 'expired'].includes(scenario) ? currentCsrf : value;
    assert.equal(controls[name].value, expected);
  }
  assert.equal(calls[0].options.body.get('csrf_token'), 'test-csrf', 'The sent snapshot must not be changed');
  assert.equal(preview.src, created[0]);
  assert.equal(error.hidden, false);
  assert.equal(navigations.length, 0);
  assert.equal(submit.textContent, '保存する');
  assert.equal(submit.disabled, false);
  assert.equal(photo.disabled, false);
  assert.equal(originalDisabled.disabled, true);
  if (scenario === 'validation') {
    assert.match(elements.get('shop-url-error').textContent, /有効なURL/);
    assert.equal(controls.area.getAttribute('aria-describedby'), 'shop-area-help shop-area-error');
    assert.equal(controls.url.getAttribute('aria-invalid'), 'true');
    controls.url.value = 'https://example.com/corrected'; controls.area.value = '銀座';
  } else if (scenario === 'photo') {
    assert.match(elements.get('shop-photo-error').textContent, /読み取れません/);
    assert.equal(photo.getAttribute('aria-describedby'), 'shop-photo-help shop-photo-error');
    await form.listeners.submit(event);
    assert.equal(calls[1].options.body.get('photo'), firstFile);
    assert.equal(error.hidden, false);
    photo.files = [secondFile]; photo.listeners.change();
  } else {
    assert.equal(error.focusCount, 1);
    if (scenario === 'auth') {
      assert.match(error.textContent, /認証情報が更新されました。もう一度保存/);
      assert.doesNotMatch(error.textContent, /ログインし直し/);
    } else assert.match(error.textContent, /入力内容と写真は保持/);
    if (['auth', 'expired'].includes(scenario)) {
      if (scenario === 'expired') assert.match(error.textContent, /別のタブでログイン/);
      assert.equal(controls.csrf_token.value, currentCsrf);
      authenticated = true;
    }
  }
  await form.listeners.submit(event);
  const retried = calls.at(-1).options.body;
  assert.equal(retried.get('photo'), scenario === 'photo' ? secondFile : firstFile);
  for (const [name, control] of Object.entries(controls)) assert.equal(retried.get(name), control.value);
  assert.equal(controls.area.getAttribute('aria-describedby'), 'shop-area-help');
  assert.equal(photo.getAttribute('aria-describedby'), 'shop-photo-help');
  assert.equal(controls.url.getAttribute('aria-invalid'), null);
  assert.equal(navigations.at(-1), 'https://meshi.test/shop/12?saved=true&return_to=%2F%3Fq%3Dcafe');
  events.pagehide({ persisted: false });
  assert.equal(revoked.at(-1), created.at(-1));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", harness, scenario], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
