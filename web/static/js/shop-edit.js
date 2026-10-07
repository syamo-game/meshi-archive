// @ts-check
(function () {
  'use strict';

  /** @typedef {'shop_name'|'area'|'url'|'rating'|'visited_at'|'photo'} ErrorField */
  /** @typedef {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|HTMLButtonElement} FormControl */
  /** @typedef {{control: HTMLInputElement|HTMLSelectElement, feedback: HTMLElement}} FieldElements */
  /** @typedef {{detail?: string, errors?: Partial<Record<ErrorField, string>>, redirect_url?: string}} SaveResponse */
  /** @typedef {'shop_name'|'area'|'category'|'url'|'address'|'phone'|'memo'|'rating'|'is_visited'|'visited_at'} EditableField */
  /** @typedef {Record<Exclude<EditableField, 'is_visited'>, string> & {is_visited: boolean}} EditValues */
  /** @typedef {{version: number, values: EditValues, image_url: string|null}} EditSnapshot */
  /** @typedef {{name: EditableField|'photo', control: HTMLSelectElement}} ConflictChoice */

  const form = document.getElementById('shop-edit-form');
  if (!form) return;
  if (!(form instanceof HTMLFormElement)) throw new Error('Shop edit form is invalid');

  /** @param {string} id @returns {HTMLElement} */
  function requiredElement(id) {
    const element = document.getElementById(id);
    if (!element) throw new Error('Shop edit element is missing: ' + id);
    return element;
  }

  const photo = requiredElement('shop-photo');
  const preview = requiredElement('shop-photo-preview');
  const submit = requiredElement('shop-edit-submit');
  if (!(photo instanceof HTMLInputElement) || !(preview instanceof HTMLImageElement) ||
      !(submit instanceof HTMLButtonElement)) throw new Error('Shop photo controls are invalid');
  const emptyPreview = requiredElement('shop-photo-empty');
  const previewLabel = requiredElement('shop-photo-preview-label');
  const errorBox = requiredElement('shop-edit-error');
  let currentPhoto = preview.getAttribute('data-current-src') || '';
  const expectedVersion = requiredElement('shop-expected-version');
  const conflictBox = requiredElement('shop-edit-conflicts');
  const conflictLoad = requiredElement('shop-conflict-load');
  const conflictFields = requiredElement('shop-conflict-fields');
  const conflictAccept = requiredElement('shop-conflict-accept');
  if (!(expectedVersion instanceof HTMLInputElement) || !(conflictLoad instanceof HTMLButtonElement) ||
      !(conflictAccept instanceof HTMLButtonElement)) throw new Error('Shop conflict controls are invalid');
  /** @type {Record<EditableField, string>} */
  const editLabels = {
    shop_name: '店名', area: 'エリア', category: 'カテゴリ', url: 'URL', address: '住所',
    phone: '電話番号', memo: 'メモ', rating: '評価', is_visited: '訪問済み', visited_at: '訪問日',
  };
  /** @type {Map<EditableField, HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement>} */
  const editControls = new Map();
  for (const name of /** @type {EditableField[]} */ (Object.keys(editLabels))) {
    const control = Array.from(form.elements).find(function (item) {
      return (item instanceof HTMLInputElement || item instanceof HTMLSelectElement ||
        item instanceof HTMLTextAreaElement) && item.name === name;
    });
    if (!(control instanceof HTMLInputElement) && !(control instanceof HTMLSelectElement) &&
        !(control instanceof HTMLTextAreaElement)) throw new Error('Shop edit control is missing: ' + name);
    editControls.set(name, control);
  }
  const originalLabel = submit.textContent;
  const maxPhotoBytes = 20 * 1024 * 1024;
  const csrfInput = Array.from(form.elements).find(function (control) {
    return control instanceof HTMLInputElement && control.name === 'csrf_token';
  });
  if (!(csrfInput instanceof HTMLInputElement)) throw new Error('Shop edit CSRF field is missing');
  /** @type {Record<ErrorField, string>} */
  const fieldIds = {
    shop_name: 'shop-name', area: 'shop-area', url: 'shop-url',
    rating: 'shop-rating', visited_at: 'shop-visited-at', photo: 'shop-photo',
  };
  /** @type {Map<ErrorField, FieldElements>} */
  const fields = new Map();
  for (const name of /** @type {ErrorField[]} */ (Object.keys(fieldIds))) {
    const control = requiredElement(fieldIds[name]);
    if (!(control instanceof HTMLInputElement) && !(control instanceof HTMLSelectElement)) {
      throw new Error('Shop edit field is invalid: ' + name);
    }
    fields.set(name, { control: control, feedback: requiredElement(fieldIds[name] + '-error') });
  }
  /** @type {Map<FormControl, boolean>} */
  const disabledBeforeSave = new Map();
  let busy = false;
  let previewUrl = '';
  let conflictBlocked = false;
  /** @type {EditSnapshot|null} */
  let latestSnapshot = null;
  /** @type {EditValues|null} */
  let comparedValues = null;
  /** @type {File|undefined} */
  let comparedPhoto;
  /** @type {ConflictChoice[]} */
  let conflictChoices = [];

  /** @returns {EditValues} */
  function readEditValues() {
    const values = /** @type {EditValues} */ ({});
    editControls.forEach(function (control, name) {
      if (name === 'is_visited') {
        if (!(control instanceof HTMLInputElement)) throw new Error('Shop visit checkbox is invalid');
        values.is_visited = control.checked;
      } else values[name] = control.value;
    });
    return values;
  }

  /** @returns {void} */
  function clearComparison() {
    latestSnapshot = null;
    comparedValues = null;
    comparedPhoto = undefined;
    conflictChoices = [];
    conflictFields.replaceChildren();
    conflictAccept.hidden = true;
    conflictAccept.disabled = true;
  }

  /** @returns {void} */
  function blockForConflict() {
    conflictBlocked = true;
    conflictBox.hidden = false;
    clearComparison();
    submit.disabled = true;
  }

  /** @returns {void} */
  function invalidateComparison() {
    if (!conflictBlocked || !latestSnapshot) return;
    clearComparison();
    showError('入力内容が変わりました。最新値を読み込み直して、残す値を選んでください。');
  }

  /** @param {EditableField|'photo'} name @param {string} label @param {string} input @param {string} latest @returns {void} */
  function addConflictChoice(name, label, input, latest) {
    const container = document.createElement('div');
    container.className = 'mb-3';
    const description = document.createElement('p');
    description.className = 'text-break mb-1';
    description.style.whiteSpace = 'pre-wrap';
    description.textContent = label + '\n入力: ' + input + '\n最新: ' + latest;
    const choiceLabel = document.createElement('label');
    choiceLabel.className = 'form-label';
    choiceLabel.htmlFor = 'shop-conflict-' + name;
    choiceLabel.textContent = label + 'に残す値';
    const choice = document.createElement('select');
    choice.id = choiceLabel.htmlFor;
    choice.className = 'form-select';
    for (const [value, text] of [['', '選択してください'], ['input', '入力した値を使う'], ['latest', '最新の値を使う']]) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      choice.append(option);
    }
    choice.value = '';
    conflictChoices.push({ name: name, control: choice });
    choice.addEventListener('change', function () {
      conflictAccept.disabled = conflictChoices.some(function (item) {
        return item.control.value !== 'input' && item.control.value !== 'latest';
      });
    });
    container.append(description, choiceLabel, choice);
    conflictFields.append(container);
  }

  /** @param {EditSnapshot} snapshot @returns {void} */
  function renderComparison(snapshot) {
    clearComparison();
    latestSnapshot = snapshot;
    comparedValues = readEditValues();
    comparedPhoto = photo.files ? photo.files[0] : undefined;
    for (const name of /** @type {EditableField[]} */ (Object.keys(editLabels))) {
      if (comparedValues[name] === snapshot.values[name]) continue;
      const input = comparedValues[name];
      const latest = snapshot.values[name];
      addConflictChoice(name, editLabels[name], typeof input === 'boolean' ? (input ? '訪問済み' : '未訪問') : input || '（空欄）',
        typeof latest === 'boolean' ? (latest ? '訪問済み' : '未訪問') : latest || '（空欄）');
    }
    if (comparedPhoto) addConflictChoice('photo', '写真', comparedPhoto.name, snapshot.image_url ? '保存されている最新の写真' : '写真なし');
    if (snapshot.image_url) {
      const latestImage = document.createElement('img');
      latestImage.src = snapshot.image_url;
      latestImage.alt = '保存されている最新の写真';
      latestImage.className = 'img-fluid mb-3';
      conflictFields.append(latestImage);
    }
    if (!conflictChoices.length) {
      const description = document.createElement('p');
      description.textContent = '入力内容と最新の値は同じです。確認後、編集を再開できます。';
      conflictFields.append(description);
    }
    conflictAccept.hidden = false;
    conflictAccept.disabled = conflictChoices.length > 0;
  }

  /** @param {object} payload @returns {payload is EditSnapshot} */
  function isEditSnapshot(payload) {
    if (!payload || Array.isArray(payload) || !('version' in payload) || typeof payload.version !== 'number' ||
        !Number.isInteger(payload.version) || payload.version < 1 || !('values' in payload) ||
        !payload.values || typeof payload.values !== 'object' || Array.isArray(payload.values) ||
        !('image_url' in payload) || (payload.image_url !== null && typeof payload.image_url !== 'string')) return false;
    const values = payload.values;
    return Object.keys(editLabels).every(function (name) {
      return name in values && typeof values[/** @type {keyof typeof values} */ (name)] === (name === 'is_visited' ? 'boolean' : 'string');
    });
  }

  /** @param {ErrorField} name @param {string|null} message @returns {void} */
  function setFieldError(name, message) {
    const field = fields.get(name);
    if (!field) throw new Error('Shop edit error field is missing: ' + name);
    field.feedback.textContent = message || '';
    field.control.classList.toggle('is-invalid', Boolean(message));
    const descriptions = (field.control.getAttribute('aria-describedby') || '').split(/\s+/)
      .filter(function (id) { return id && id !== field.feedback.id; });
    if (message) {
      field.control.setAttribute('aria-invalid', 'true');
      descriptions.push(field.feedback.id);
    } else {
      field.control.removeAttribute('aria-invalid');
    }
    if (descriptions.length) field.control.setAttribute('aria-describedby', descriptions.join(' '));
    else field.control.removeAttribute('aria-describedby');
  }

  /** @returns {void} */
  function clearErrors() {
    fields.forEach(function (_field, name) { setFieldError(name, null); });
    errorBox.textContent = '';
    errorBox.hidden = true;
  }

  /** @param {string} message @returns {void} */
  function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = false;
  }

  /** @param {number} status @param {string|undefined} detail @returns {string} */
  function failureMessage(status, detail) {
    if (status === 403 && typeof detail === 'string' && /[\u3040-\u30ff\u3400-\u9fff]/.test(detail)) return detail;
    if (status === 401 || status === 403) {
      return 'ログイン状態を確認できませんでした。この画面を開いたまま、別のタブでログインし直してください。入力内容と写真は保持されています。';
    }
    if (status === 404) {
      return 'この店舗が見つかりません。別の画面で店舗情報を確認してください。入力内容と写真は保持されています。';
    }
    if (status === 409) {
      return '店舗情報が更新されています。入力内容と写真は保持しています。「最新値と比較」で残す値を選んでください。';
    }
    if (typeof detail === 'string' && /[\u3040-\u30ff\u3400-\u9fff]/.test(detail)) return detail;
    if (status === 400) return '入力内容を確認してください。入力内容と写真は保持されています。';
    return '保存できませんでした。入力内容と写真は保持されています。もう一度「保存する」でお試しください。';
  }

  /** @returns {void} */
  function focusError() {
    const invalid = Array.from(fields.values()).find(function (field) {
      return field.control.getAttribute('aria-invalid') === 'true';
    });
    const target = invalid ? invalid.control : errorBox;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: 'nearest' });
  }

  /** @param {boolean} saving @returns {void} */
  function setBusy(saving) {
    busy = saving;
    form.setAttribute('aria-busy', String(saving));
    if (saving) {
      for (const control of Array.from(form.elements)) {
        if (control instanceof HTMLInputElement || control instanceof HTMLSelectElement ||
            control instanceof HTMLTextAreaElement || control instanceof HTMLButtonElement) {
          disabledBeforeSave.set(control, control.disabled);
          control.disabled = true;
        }
      }
      submit.textContent = '保存中…';
      submit.setAttribute('aria-busy', 'true');
    } else {
      disabledBeforeSave.forEach(function (disabled, control) { control.disabled = disabled; });
      disabledBeforeSave.clear();
      submit.textContent = originalLabel;
      submit.removeAttribute('aria-busy');
      if (conflictBlocked) submit.disabled = true;
    }
  }

  /** @param {File|undefined} file @returns {string|null} */
  function photoError(file) {
    if (!file) return null;
    if (file.size > maxPhotoBytes) return '写真は20MiB以下のファイルを選んでください。';
    if (file.type && !['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
      return 'JPEG・PNG・WebPの写真を選んでください。';
    }
    return null;
  }

  /** @returns {void} */
  function releasePreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = '';
  }

  /** @returns {void} */
  function updatePreview() {
    releasePreview();
    const file = photo.files ? photo.files[0] : undefined;
    const message = photoError(file);
    setFieldError('photo', message);
    if (message) {
      preview.removeAttribute('src');
      preview.hidden = true;
      emptyPreview.hidden = false;
      emptyPreview.textContent = '選択した写真を表示できません。';
      previewLabel.textContent = '写真の形式とサイズを確認してください。';
      return;
    }
    if (file) previewUrl = URL.createObjectURL(file);
    const source = previewUrl || currentPhoto;
    if (source) preview.src = source;
    else preview.removeAttribute('src');
    preview.hidden = !source;
    emptyPreview.hidden = Boolean(source);
    emptyPreview.textContent = '写真は登録されていません。';
    previewLabel.textContent = file ? '選択した写真（保存前）'
      : (currentPhoto ? '現在の写真' : '写真を選ぶと、保存前に確認できます。');
  }

  photo.addEventListener('change', function () {
    updatePreview();
    invalidateComparison();
  });
  for (const eventName of ['input', 'change']) {
    form.addEventListener(eventName, function (event) {
      if (Array.from(editControls.values()).some(function (control) { return control === event.target; })) invalidateComparison();
    });
  }
  conflictLoad.addEventListener('click', async function () {
    if (busy || !conflictBlocked) return;
    clearComparison();
    clearErrors();
    setBusy(true);
    submit.textContent = '最新値を取得中…';
    let status = 0;
    try {
      const endpoint = form.action.replace(/\/edit$/, '/edit-snapshot');
      const response = await fetch(endpoint, {
        method: 'GET', cache: 'no-store', credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' },
      });
      status = response.status;
      if (!response.ok) throw new Error('Shop snapshot request failed: status=' + status);
      /** @type {object} */
      const payload = await response.json();
      if (!isEditSnapshot(payload)) throw new Error('Shop snapshot response is invalid');
      renderComparison(payload);
    } catch (error) {
      console.error('Shop snapshot load failed', { endpoint: form.action, status: status, error: error });
      showError('最新値を取得できませんでした。入力内容と写真は保持しています。ログインや接続を確認して、もう一度比較してください。');
    } finally {
      setBusy(false);
      // Newly created choices were not part of the disabled-control snapshot.
      conflictAccept.disabled = !latestSnapshot || conflictChoices.some(function (item) {
        return item.control.value !== 'input' && item.control.value !== 'latest';
      });
      if (!errorBox.hidden) focusError();
    }
  });
  conflictAccept.addEventListener('click', function () {
    if (busy || !conflictBlocked || !latestSnapshot || !comparedValues || conflictChoices.some(function (item) {
      return item.control.value !== 'input' && item.control.value !== 'latest';
    })) return;
    const currentValues = readEditValues();
    if (Object.keys(editLabels).some(function (name) {
      const field = /** @type {EditableField} */ (name);
      return !comparedValues || currentValues[field] !== comparedValues[field];
    }) || (photo.files ? photo.files[0] : undefined) !== comparedPhoto) {
      invalidateComparison();
      return;
    }
    const accepted = latestSnapshot;
    for (const choice of conflictChoices) {
      if (choice.control.value !== 'latest') continue;
      if (choice.name === 'photo') {
        photo.value = '';
        continue;
      }
      const control = editControls.get(choice.name);
      if (!control) throw new Error('Shop conflict field is missing: ' + choice.name);
      if (choice.name === 'is_visited') {
        if (!(control instanceof HTMLInputElement)) throw new Error('Shop visit checkbox is invalid');
        control.checked = accepted.values.is_visited;
      } else {
        const value = accepted.values[choice.name];
        if (control instanceof HTMLSelectElement && !Array.from(control.options).some(function (option) { return option.value === value; })) {
          const option = document.createElement('option');
          option.value = value;
          option.textContent = value || '未設定';
          control.append(option);
        }
        control.value = value;
      }
    }
    currentPhoto = accepted.image_url || '';
    preview.setAttribute('data-current-src', currentPhoto);
    expectedVersion.value = String(accepted.version);
    conflictBlocked = false;
    conflictBox.hidden = true;
    clearComparison();
    clearErrors();
    updatePreview();
    submit.disabled = false;
    submit.focus({ preventScroll: true });
  });
  preview.addEventListener('error', function () {
    console.error('Shop photo preview failed', { source: preview.getAttribute('src') });
    preview.hidden = true;
    emptyPreview.hidden = false;
    emptyPreview.textContent = '写真のプレビューを表示できません。';
    if (previewUrl) setFieldError('photo', '写真を表示できません。画像ファイルを確認してください。');
  });
  window.addEventListener('pagehide', function (event) {
    if (!event.persisted) releasePreview();
  });

  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (busy) return;
    if (conflictBlocked) {
      showError('最新値と比較し、残す値を選んでから編集を再開してください。入力内容と写真は保持しています。');
      focusError();
      return;
    }
    clearErrors();
    const selectedPhoto = photo.files ? photo.files[0] : undefined;
    const localPhotoError = photoError(selectedPhoto);
    if (localPhotoError) {
      setFieldError('photo', localPhotoError);
      showError('入力内容を確認してください。変更内容はまだ保存されていません。');
      focusError();
      return;
    }
    // Capture values and the File before disabling the form.
    const snapshot = new FormData(form);
    setBusy(true);
    let status = 0;
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: snapshot, credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json' },
      });
      status = response.status;
      if (status === 409) blockForConflict();
      if (status === 401 || status === 403) {
        const refreshedToken = response.headers.get('X-CSRF-Token');
        if (refreshedToken) csrfInput.value = refreshedToken;
      }
      /** @type {SaveResponse} */
      const payload = await response.json();
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error('Shop edit response must be a JSON object');
      }
      if (!response.ok) {
        console.error('Shop edit request failed', { endpoint: form.action, status: status, response: payload });
        if (payload.errors && typeof payload.errors === 'object' && !Array.isArray(payload.errors)) {
          fields.forEach(function (_field, name) {
            const message = payload.errors ? payload.errors[name] : null;
            if (typeof message === 'string') setFieldError(name, message);
          });
        }
        showError(failureMessage(status, payload.detail));
        return;
      }
      if (typeof payload.redirect_url !== 'string' || !payload.redirect_url) {
        throw new Error('Shop edit response is missing redirect_url');
      }
      const destination = new URL(payload.redirect_url, window.location.href);
      if (destination.origin !== window.location.origin) throw new Error('Shop edit redirect must stay on this site');
      window.location.assign(destination.href);
    } catch (error) {
      console.error('Shop edit save failed', { endpoint: form.action, status: status, error: error });
      showError('保存結果を確認できませんでした。入力内容と写真は保持されています。接続状況を確認して、もう一度「保存する」でお試しください。');
    } finally {
      setBusy(false);
      if (!errorBox.hidden) focusError();
    }
  });
}());
