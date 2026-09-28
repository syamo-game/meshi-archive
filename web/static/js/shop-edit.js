// @ts-check
(function () {
  'use strict';

  /** @typedef {'shop_name'|'area'|'url'|'rating'|'visited_at'|'photo'} ErrorField */
  /** @typedef {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement|HTMLButtonElement} FormControl */
  /** @typedef {{control: HTMLInputElement|HTMLSelectElement, feedback: HTMLElement}} FieldElements */
  /** @typedef {{detail?: string, errors?: Partial<Record<ErrorField, string>>, redirect_url?: string}} SaveResponse */

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
  const currentPhoto = preview.getAttribute('data-current-src') || '';
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
      return '店舗情報が更新されています。別の画面で最新の内容を確認してください。入力内容と写真は保持されています。';
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

  photo.addEventListener('change', updatePreview);
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
