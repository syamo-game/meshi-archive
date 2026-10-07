from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("scenario", [
    "sequence", "double_submit", "rejected_save", "lost_save", "bad_reply", "auth",
    "timeout", "invalid_file", "readonly", "no_changes",
])
def test_bulk_csv_workflow_retains_bytes_and_handles_failures(scenario: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {File} = require('node:buffer');
const scenario = process.argv[2];
class Element {
  constructor(id = '') { this.id = id; this.children = []; this.attributes = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.textContent = ''; this.focusCount = 0; this.listeners = {}; }
  get childNodes() { return this.children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  hasAttribute(key) { return key in this.attributes; }
  focus() { this.focusCount++; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  replaceChildren(...children) { this.children = children; }
  matches(selector) { return selector.startsWith('#') ? this.id === selector.slice(1) : selector.startsWith('[') ? this.hasAttribute(selector.slice(1,-1)) : selector === 'h2' && this.tag === 'h2'; }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(selector.split(', ').some(s => child.matches(s)) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(element) { return this.children.includes(element) || this.children.some(child => child.contains(element)); }
  closest(selector) { return selector.split(', ').some(s => this.matches(s)) ? this : null; }
  dispatch(type, target = this) { const event = {target, preventDefault(){}}; for (const fn of this.listeners[type] || []) fn(event); }
}
class Form extends Element {}
class Button extends Element {}
class Input extends Element { constructor(id) { super(id); this.files = []; this.value = ''; } }
const workspace = new Element('bulk-workspace'), selection = new Element('bulk-selection'), content = new Element('bulk-content'), errors = new Element('bulk-errors');
const form = new Form('bulk-file-form'), input = new Input('bulk-file-input'), check = new Button('bulk-check'), token = new Input('bulk-csrf-token'), download = new Element('bulk-download');
workspace.dataset.readOnly = scenario === 'readonly' ? 'true' : 'false';
errors.hidden = true; token.value = 'synthetic-csrf';
const elements = new Map([workspace, selection, content, errors, form, input, check, token, download].map(e => [e.id,e]));
const document = {getElementById: id => elements.get(id) || null, createElement: () => new Element()};
function responsePage(kind) {
  const nextErrors = new Element('bulk-errors'), nextContent = new Element('bulk-content'), nextToken = new Input('bulk-csrf-token'); nextToken.value = 'synthetic-csrf';
  if (kind === 'errors') { nextErrors.textContent = '2行目：最新のCSVをダウンロードしてください。'; const p = new Element(); p.textContent = nextErrors.textContent; nextErrors.children = [p]; }
  else if (kind !== 'bad') {
    const stage = new Element(); stage.dataset.bulkStage = kind; stage.setAttribute('data-bulk-stage', kind);
    const heading = new Element(); heading.tag = 'h2'; stage.children = [heading];
    const back = new Button(); back.setAttribute(kind === 'complete' ? 'data-bulk-again' : 'data-bulk-back',''); stage.children.push(back);
    if (kind === 'preview') { const saveForm = new Form('bulk-save-form'); if (scenario !== 'no_changes') saveForm.children = [new Button('bulk-save')]; stage.children.push(saveForm); }
    nextContent.children = [stage];
  }
  return {getElementById: id => id === 'bulk-errors' ? nextErrors : id === 'bulk-content' ? nextContent : id === 'bulk-csrf-token' ? nextToken : null};
}
class DOMParser { parseFromString(text) { return responsePage(text); } }
const calls = [], logs = [];
let resolveRequest, timeoutCallback;
async function fetchRequest(endpoint, options) {
  const file = options.body.get('file');
  calls.push({endpoint, file, csrf:options.body.get('csrf_token'), signal:options.signal});
  if (scenario === 'double_submit' && calls.length === 1) await new Promise(resolve => { resolveRequest = resolve; });
  const saving = endpoint.endsWith('/apply');
  if (saving && scenario === 'lost_save') throw new Error('Synthetic response loss');
  if (saving && scenario === 'timeout') await new Promise((resolve,reject) => options.signal.addEventListener('abort',()=>reject(new Error('Synthetic timeout'))));
  const rejected = saving && scenario === 'rejected_save';
  return {ok:!rejected,status:scenario === 'auth' ? 403 : rejected ? 409 : 200, redirected:false,text:async()=>scenario === 'bad_reply' ? 'bad' : rejected ? 'errors' : saving ? 'complete' : 'preview'};
}
vm.runInNewContext(fs.readFileSync('web/static/js/admin-bulk.js','utf8'), {
  document, HTMLElement:Element, Element, HTMLFormElement:Form, HTMLInputElement:Input, HTMLButtonElement:Button,
  File, FormData, AbortController, DOMParser, fetch:fetchRequest, Error,
  console:{error:(...args)=>logs.push(args)}, window:{setTimeout(fn){timeoutCallback=fn;return 1;},clearTimeout(){}},
});
const flush = async()=>{ for(let i=0;i<5;i++) await new Promise(resolve=>setImmediate(resolve)); };
async function confirm() { form.dispatch('submit'); await flush(); }
async function save() { content.dispatch('submit',content.querySelector('#bulk-save-form')); await flush(); }
(async()=>{
  if (scenario === 'invalid_file') {
    await confirm(); assert.equal(calls.length,0); assert.equal(input.attributes['aria-invalid'],'true');
    input.files=[new File(['x'],'bad.txt')]; await confirm(); assert.equal(calls.length,0);
    input.files=[new File([new Uint8Array(5*1024*1024+1)],'large.csv')]; await confirm(); assert.equal(calls.length,0); assert.equal(errors.hidden,false); return;
  }
  const source = new File(['original,csv\n1,test\n'],'synthetic.csv',{type:'text/csv'});
  input.files=[source]; input.dispatch('change');
  if (scenario === 'readonly') { await confirm(); assert.equal(calls.length,0); assert.equal(check.disabled,true); assert.equal(input.disabled,true); return; }
  if (scenario === 'double_submit') {
    form.dispatch('submit'); await flush(); assert.equal(check.disabled,true); assert.equal(input.disabled,true);
    form.dispatch('submit'); await flush(); assert.equal(calls.length,1); resolveRequest(); await flush();
  } else await confirm();
  assert.equal(calls[0].csrf,'synthetic-csrf');
  assert.equal(await calls[0].file.text(),await source.text());
  assert.notEqual(calls[0].file,source,'Confirmation must freeze the selected bytes');
  if (['bad_reply','auth'].includes(scenario)) { assert.equal(selection.hidden,false); assert.equal(errors.hidden,false); assert.equal(logs.length,1); assert.equal(content.querySelector('#bulk-save'),null); return; }
  assert.equal(selection.hidden,true); assert.equal(content.querySelector('h2').focusCount,1);
  if (scenario === 'no_changes') { assert.equal(content.querySelector('#bulk-save'),null); content.dispatch('click',content.querySelector('[data-bulk-back]')); assert.equal(selection.hidden,false); return; }
  if (scenario === 'sequence') {
    content.dispatch('click',content.querySelector('[data-bulk-back]')); assert.equal(selection.hidden,false); assert.equal(input.focusCount,1);
    input.files=[new File(['changed outside the page'],'synthetic.csv')];
    await confirm(); assert.equal(calls[1].file,calls[0].file,'Back must retain the exact confirmed bytes');
  }
  await save();
  if (scenario === 'timeout') { timeoutCallback(); await flush(); assert.equal(calls.at(-1).signal.aborted,true); }
  const last = calls.at(-1); assert.equal(last.endpoint,'/admin/import/update/apply'); assert.equal(last.file,calls[0].file);
  if (['lost_save','timeout','rejected_save'].includes(scenario)) {
    assert.equal(errors.hidden,false); assert.equal(content.querySelector('#bulk-save').disabled,true);
    assert.equal(content.querySelector('[data-bulk-back]').disabled,false);
    const count = calls.length; await save(); assert.equal(calls.length,count,'A failed save must not be replayed');
    assert.equal(logs.length,1);
    content.dispatch('click',content.querySelector('[data-bulk-back]')); input.dispatch('change'); await confirm(); assert.equal(content.querySelector('#bulk-save').disabled,false); return;
  }
  assert.equal(content.querySelector('[data-bulk-stage]').dataset.bulkStage,'complete');
  content.dispatch('click',content.querySelector('[data-bulk-again]')); assert.equal(selection.hidden,false); assert.equal(content.children.length,0); assert.equal(download.focusCount,1);
  input.files=[new File(['next,csv'],'next.csv')]; input.dispatch('change'); await confirm(); assert.equal(await calls.at(-1).file.text(),'next,csv');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    path = tmp_path / "bulk-flow.cjs"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(path), scenario], capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
