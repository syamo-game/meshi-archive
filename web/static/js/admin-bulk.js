// Keep one immutable CSV for both confirmation and saving.
(function () {
  'use strict';

  const workspace = document.getElementById('bulk-workspace');
  const selection = document.getElementById('bulk-selection');
  const content = document.getElementById('bulk-content');
  const errors = document.getElementById('bulk-errors');
  const form = document.getElementById('bulk-file-form');
  const input = document.getElementById('bulk-file-input');
  const check = document.getElementById('bulk-check');
  const token = document.getElementById('bulk-csrf-token');
  if (!(workspace instanceof HTMLElement) || !(selection instanceof HTMLElement) ||
      !(content instanceof HTMLElement) || !(errors instanceof HTMLElement) ||
      !(form instanceof HTMLFormElement) || !(input instanceof HTMLInputElement) ||
      !(check instanceof HTMLButtonElement) || !(token instanceof HTMLInputElement)) {
    throw new Error('Missing bulk CSV controls');
  }

  /** @type {File | null} */
  let selectedFile = null;
  /** @type {boolean} */
  let busy = false;
  /** @type {boolean} */
  let saveBlocked = false;
  const readOnly = workspace.dataset.readOnly === 'true';

  /** @returns {void} */
  function updateControls() {
    input.disabled = busy || readOnly;
    check.disabled = busy || readOnly;
    check.textContent = busy && !selection.hidden ? '確認中…' : '変更内容を確認';
    const save = content.querySelector('#bulk-save');
    if (save instanceof HTMLButtonElement) {
      save.disabled = busy || readOnly || saveBlocked || !selectedFile;
      save.textContent = busy ? '保存中…' : '保存';
    }
    for (const button of content.querySelectorAll('[data-bulk-back], [data-bulk-again]')) {
      if (button instanceof HTMLButtonElement) button.disabled = busy;
    }
    workspace.setAttribute('aria-busy', String(busy));
  }

  /** @returns {void} */
  function clearErrors() {
    errors.replaceChildren();
    errors.hidden = true;
    input.removeAttribute('aria-invalid');
  }

  /** @param {string} message @returns {void} */
  function showError(message) {
    const paragraph = document.createElement('p');
    paragraph.className = 'alert alert-danger';
    paragraph.setAttribute('role', 'alert');
    paragraph.textContent = message;
    errors.replaceChildren(paragraph);
    errors.hidden = false;
    errors.focus();
  }

  /** @param {boolean} restart @returns {void} */
  function returnToSelection(restart) {
    content.replaceChildren();
    selection.hidden = false;
    saveBlocked = false;
    if (restart) {
      selectedFile = null;
      input.value = '';
      clearErrors();
    }
    updateControls();
    const target = restart ? document.getElementById('bulk-download') : input;
    if (target instanceof HTMLElement) target.focus();
  }

  /** @param {boolean} saving @returns {Promise<void>} */
  async function submitCsv(saving) {
    if (busy || readOnly || (saving && saveBlocked)) return;
    if (!saving && !selectedFile) {
      const file = input.files && input.files[0];
      if (!file || !file.name.toLowerCase().endsWith('.csv') || file.size > 5 * 1024 * 1024) {
        showError(!file ? '編集したCSVを選択してください。' : file.size > 5 * 1024 * 1024 ? 'CSVファイルは5 MB以下にしてください。' : 'CSVファイルを選択してください。');
        input.setAttribute('aria-invalid', 'true');
        input.focus();
        return;
      }
    }
    if (saving && !selectedFile) {
      showError('ファイルを保持できませんでした。戻ってCSVを選択し直してください。');
      return;
    }
    busy = true;
    clearErrors();
    updateControls();
    const endpoint = saving ? '/admin/import/update/apply' : '/admin/import/validate';
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 45000);
    /** @type {number | null} */
    let status = null;
    try {
      if (!selectedFile) {
        const source = input.files && input.files[0];
        if (!source) throw new Error('CSV disappeared before confirmation');
        selectedFile = new File([await source.arrayBuffer()], source.name, {type: 'text/csv'});
      }
      const body = new FormData();
      body.append('file', selectedFile);
      body.append('csrf_token', token.value);
      const response = await fetch(endpoint, {method: 'POST', body, credentials: 'same-origin', signal: controller.signal});
      status = response.status;
      if (response.redirected || response.status === 401 || response.status === 403) {
        throw new Error('CSV access refused or redirected');
      }
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const nextErrors = page.getElementById('bulk-errors');
      const nextContent = page.getElementById('bulk-content');
      if (!(nextErrors instanceof HTMLElement) || !(nextContent instanceof HTMLElement)) {
        throw new Error('CSV response did not contain the expected view');
      }
      const nextToken = page.getElementById('bulk-csrf-token');
      if (nextToken instanceof HTMLInputElement) token.value = nextToken.value;
      if (!response.ok) {
        if (!nextErrors.textContent.trim()) throw new Error('CSV error response has no explanation');
        errors.replaceChildren(...nextErrors.childNodes);
        errors.hidden = false;
        if (saving) saveBlocked = true;
        console.error('Bulk CSV rejected', {endpoint, status});
        errors.focus();
        return;
      }
      const stage = nextContent.querySelector('[data-bulk-stage]');
      if (!(stage instanceof HTMLElement) || stage.dataset.bulkStage !== (saving ? 'complete' : 'preview')) {
        throw new Error('CSV response has an unexpected stage');
      }
      content.replaceChildren(...nextContent.childNodes);
      selection.hidden = true;
      saveBlocked = false;
      if (saving) selectedFile = null;
      const heading = content.querySelector('h2');
      if (heading instanceof HTMLElement) heading.focus();
    } catch (error) {
      if (saving) saveBlocked = true;
      console.error('Bulk CSV request failed', {endpoint, status, error});
      showError(status === 401 || status === 403 || (error instanceof Error && error.message.includes('access refused'))
        ? '認証または操作権限を確認できません。ページを開き直してください。'
        : saving ? '保存結果を確認できませんでした。戻って最新のCSVをダウンロードし、保存結果を確認してください。'
        : 'CSVを確認できませんでした。通信状態を確認して、もう一度お試しください。');
    } finally {
      window.clearTimeout(timeout);
      busy = false;
      updateControls();
    }
  }

  input.addEventListener('change', function () {
    selectedFile = null;
    saveBlocked = false;
    clearErrors();
  });
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    void submitCsv(false);
  });
  content.addEventListener('submit', function (event) {
    if (!(event.target instanceof HTMLFormElement) || event.target.id !== 'bulk-save-form') return;
    event.preventDefault();
    if (content.querySelector('#bulk-save')) void submitCsv(true);
  });
  content.addEventListener('click', function (event) {
    if (busy || !(event.target instanceof Element)) return;
    const button = event.target.closest('[data-bulk-back], [data-bulk-again]');
    if (!(button instanceof HTMLButtonElement) || !content.contains(button)) return;
    returnToSelection(button.hasAttribute('data-bulk-again'));
  });
  updateControls();
}());
