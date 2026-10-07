(function () {
  'use strict';

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
  var state = { items: [], selectedIndex: -1, nextCursor: null, busy: false, scope: 'all', filterQuery: '' };
  /** @typedef {{shop_name: string, branch_name: string, area: string, category: string, address: string, phone: string, canonical_url: string}} EditValues */
  /** @typedef {{values: EditValues, baseValues: EditValues, conflicts: Partial<EditValues>, expectedVersion: number, shopVersion: number|null, shopId: number|null}} EditDraft */
  /** @type {Map<string, EditDraft>} */
  const editDrafts = new Map();
  const fieldLabels = {
    shop_name: '店名', branch_name: '支店名', area: 'エリア', category: 'カテゴリ',
    address: '住所', phone: '電話番号', canonical_url: '店舗URL',
  };
  const disabledBeforeBusy = new Map();
  const labelsBeforeBusy = new Map();
  const candidateButtons = [];
  const scopeLabels = { all: 'すべての確認項目', identity: '店・支店を確認', metadata: 'エリア・カテゴリを確認' };
  const statusLabels = {
    unresolved: '未確認', pending: '未確認', deferred: 'あとで確認', approved: '確認済み', rejected: '対象外',
  };
  const reasonLabels = new Map([
    ['event_excluded', '催事会場は登録対象外です。出店元の常設店舗を特定できる根拠が不足しています。'],
    ['legacy_review', '以前のデータで要確認に設定されていました。'],
    ['source_unavailable', '元の投稿を確認できなかったため、確認対象になりました。'],
    ['source_restored', '以前取得できなかった元の投稿を再取得しました。'],
    ['extraction_not_found', '投稿から店名を特定できませんでした。'],
    ['image_unverified', '画像を手がかりに調べましたが、店舗を確定できませんでした。'],
    ['new_ambiguous', '取り込み時に店舗を確定できませんでした。'],
    ['new_pipeline_not_restaurant', '再確認では、飲食店を紹介する投稿と判定されませんでした。'],
    ['new_pipeline_mention', '再確認で、元の登録に対応しない店舗の記載が見つかりました。'],
    ['new_pipeline_difference', '再確認した店舗情報と登録内容に違いが見つかりました。'],
    ['new_pipeline_missing_mention', '再確認で、この登録に対応する店舗の記載を見つけられませんでした。'],
    ['missing_area', '確認時に、エリアが設定されていませんでした。'],
    ['unknown_area', '確認時のエリアが、登録できる地名の一覧にありませんでした。'],
    ['missing_category', '確認時に、カテゴリが設定されていませんでした。'],
    ['unknown_category', '確認時のカテゴリが、登録できる分類の一覧にありませんでした。'],
    ['new_pipeline_metadata_difference', '再確認したカテゴリと登録内容に違いが見つかりました。'],
  ]);
  const provenanceLabels = new Map([
    ['posted_url', '投稿内のリンク'], ['structured_data', 'Webページの店舗データ'],
    ['web_search', 'Web検索'], ['image', '投稿画像の解析'],
  ]);
  const assetLabels = new Map([
    ['image', '投稿の画像を開く'], ['link', '投稿のリンクを開く'], ['embed', '投稿の関連情報を開く'],
  ]);
  let retryAction = null;
  let busyOperation = 0;
  let focusWhenReady = false;

  var workspace = document.querySelector('.review-workspace');
  var queue = document.getElementById('review-queue');
  var filter = document.getElementById('review-filter');
  var errorBox = document.getElementById('review-error');
  var errorText = document.getElementById('review-error-text');
  const retryButton = document.getElementById('review-retry');
  var noticeBox = document.getElementById('review-notice');
  var noticeText = document.getElementById('review-notice-text');
  var loadMore = document.getElementById('review-load-more');
  var evidence = document.getElementById('review-evidence');
  var decisionPanel = document.getElementById('review-decision');
  var empty = document.getElementById('review-empty');
  const editSubmit = document.getElementById('review-edit-submit');
  const filterSubmit = document.getElementById('review-filter-submit');
  const mergeSubmit = document.getElementById('review-merge-submit');
  const linkForm = document.getElementById('review-link-form');
  const linkPreviewButton = document.getElementById('review-link-preview-submit');
  const linkApplyButton = document.getElementById('review-link-apply');
  const linkConfirm = document.getElementById('review-link-confirm');
  let linkPreview = null;
  let linkGeneration = 0;
  let linkLoading = false;
  let linkItemKey = null;

  if (!workspace || !queue || !filter || !errorBox || !errorText || !retryButton || !noticeBox || !noticeText || !loadMore || !evidence || !decisionPanel || !empty) return;

  workspace.tabIndex = 0;

  function setError(message, retry, retryLabel = '再試行') {
    focusWhenReady = false;
    errorText.textContent = message;
    errorBox.hidden = false;
    retryAction = retry || null;
    retryButton.hidden = !retryAction;
    retryButton.textContent = retryLabel;
    if (labelsBeforeBusy.has(retryButton)) labelsBeforeBusy.set(retryButton, retryLabel);
    errorBox.focus({ preventScroll: true });
    errorBox.scrollIntoView({ block: 'nearest' });
  }

  function clearError() {
    errorText.textContent = '';
    errorBox.hidden = true;
    retryAction = null;
  }

  function showNotice(message) {
    noticeText.textContent = message;
    noticeBox.hidden = false;
  }

  function clearNotice() {
    noticeText.textContent = '';
    noticeBox.hidden = true;
  }

  function nextRemainingItemId(items, currentIndex, removedIds) {
    var index;
    for (index = currentIndex + 1; index < items.length; index += 1) {
      if (!removedIds.has(items[index].id)) return items[index].id;
    }
    for (index = currentIndex - 1; index >= 0; index -= 1) {
      if (!removedIds.has(items[index].id)) return items[index].id;
    }
    return null;
  }

  function isEventExcluded(item) {
    return Boolean(item && !item.shop && item.review_status === 'rejected' && item.difference_type === 'event_excluded');
  }

  function effectiveScope(item) {
    if (state.scope !== 'all') return state.scope;
    return 'identity';
  }

  function canApproveCurrent(item) {
    if (!item || hasUnsavedEdits(item) || isEventExcluded(item)) return false;
    if (effectiveScope(state.items[state.selectedIndex]) === 'metadata') return Boolean(item.shop);
    return Boolean(item.shop)
      || (item.difference_type !== 'extraction_not_found' && item.extracted_name !== '（店舗名未特定）');
  }

  function updateApprovalState() {
    const item = state.items[state.selectedIndex];
    const form = document.getElementById('review-edit-form');
    const readOnly = workspace.dataset.readOnly === 'true';
    const requiredComplete = ['shop_name', 'area', 'category'].every(function (name) { return form.elements[name].value.trim(); });
    editSubmit.disabled = state.busy || readOnly || !item || !requiredComplete || Boolean(item && hasEditConflicts(item))
      || (isEventExcluded(item) && !hasUnsavedEdits(item)) || !validPhotoSelection(item);
    document.getElementById('review-reject').disabled = state.busy || readOnly || !item;
    const ai = document.getElementById('review-ai-search');
    ai.disabled = state.busy || readOnly || !item || item.review_status === 'rejected' || emptyComplementFields().length === 0;
    ai.title = !item ? '' : item.review_status === 'rejected' ? '削除済みの項目は再読み込みできません' : (emptyComplementFields().length ? '投稿と補足から空欄を入力します' : '再読み込みする空欄がありません');
    candidateButtons.forEach(function (button) { button.disabled = state.busy || readOnly || isEventExcluded(item); });
    if (readOnly) [form, document.getElementById('review-merge-form'), linkForm].forEach(function (container) { container.querySelectorAll('input, select, textarea, button').forEach(function (control) { control.disabled = true; }); });
    updateLinkControls();
    renderPhotoSelection(item);
  }

  function lockBusyControls() {
    [filter, workspace, errorBox].forEach(function (container) {
      container.querySelectorAll('button, input, select, textarea').forEach(function (control) {
        if (!disabledBeforeBusy.has(control)) disabledBeforeBusy.set(control, control.disabled);
        control.disabled = true;
      });
    });
  }

  /** @param {HTMLButtonElement} button @returns {Element} */
  function busyLabel(button) {
    return button.querySelector('[data-busy-label]') || button;
  }

  /** @param {boolean} busy @param {HTMLButtonElement|null} button @param {string} label @param {number} operation @returns {number} */
  function setBusy(busy, button = null, label = '', operation = 0) {
    if (busy) busyOperation += 1;
    else if (operation !== busyOperation) return busyOperation;
    if (busy && queue.contains(document.activeElement)) focusWhenReady = true;
    state.busy = busy;
    workspace.setAttribute('aria-busy', String(busy));
    filter.setAttribute('aria-busy', String(busy));
    if (busy) {
      lockBusyControls();
      if (button) {
        if (!labelsBeforeBusy.has(button)) labelsBeforeBusy.set(button, busyLabel(button).textContent);
        busyLabel(button).textContent = label;
        button.setAttribute('aria-busy', 'true');
      }
    } else {
      disabledBeforeBusy.forEach(function (disabled, control) { control.disabled = disabled; });
      disabledBeforeBusy.clear();
      labelsBeforeBusy.forEach(function (text, control) {
        busyLabel(control).textContent = text;
        control.removeAttribute('aria-busy');
      });
      labelsBeforeBusy.clear();
    }
    updateApprovalState();
    if (!busy) {
      if (focusWhenReady) { focusWhenReady = false; focusSelectedItem(); }
    }
    return busyOperation;
  }

  class ReviewConflictError extends Error {}
  class ReviewDecisionError extends Error {}

  function apiJson(url, options) {
    var requestOptions = options || {};
    requestOptions.headers = requestOptions.headers || {};
    if (!requestOptions.method || requestOptions.method === 'GET') requestOptions.cache = 'no-store';
    if (csrfToken && requestOptions.method && requestOptions.method !== 'GET') {
      requestOptions.headers['X-CSRF-Token'] = csrfToken;
    }
    return fetch(url, requestOptions).then(function (response) {
      return response.json().catch(function (error) {
        console.error('Review response is not valid JSON', { url: url, status: response.status, error: error });
        throw new Error('応答を読み取れませんでした（HTTP ' + response.status + '）。');
      }).then(function (payload) {
        if (!response.ok) {
          console.error('Review request failed', { url: url, method: requestOptions.method, status: response.status, response: payload });
          if (response.status === 409) {
            const detail = payload.detail || {};
            if (url.endsWith('/link')) throw new ReviewConflictError('関連付けの対象が更新されています。');
            if (['stale_mention', 'stale_shop', 'missing_shop_version'].includes(detail.code)) {
              throw new ReviewConflictError('別の更新があります。入力内容は保持されています。');
            }
            const reason = detail.code === 'shop_collision'
              ? '既存の店舗と識別情報が重複しています。対象の店舗IDと名称・住所・URLを確認してください。'
              : detail.code === 'candidate_identity_conflict'
                ? '候補の外部サービスIDが一致しません。候補の根拠URLを確認してください。'
                : detail.code === 'stale_merge_target'
                  ? '統合先の店舗が更新されています。統合先のIDを入力し直して最新の内容を確認してください。'
                  : detail.code === 'missing_shop'
                    ? '店舗が未確定です。先に「店・支店を確認」で店舗を確定してください。'
                    : detail.code === 'event_origin_required'
                      ? '催事会場は登録できません。出店元の常設店舗を根拠付きで確認してから、店舗情報の編集または既存店舗への関連付けを行ってください。'
                    : 'この操作は現在の関連データと両立しません。対象と操作内容を確認してください。';
            throw new ReviewDecisionError(reason + ' ' + (detail.message || payload.detail || ''));
          }
          if (response.status === 422 && Array.isArray(payload.detail)) {
            throw new ReviewDecisionError(payload.detail.map(function (problem) {
              const field = problem.loc[problem.loc.length - 1];
              return (fieldLabels[field] || field) + ': ' + problem.msg;
            }).join(' / '));
          }
          throw new Error(typeof payload.detail === 'string' ? payload.detail : payload.detail && payload.detail.message ? payload.detail.message : ('入力内容と接続状況を確認してください（HTTP ' + response.status + '）。'));
        }
        return payload;
      });
    });
  }

  function syncStatusOptions() {
    const metadata = document.getElementById('review-scope').value === 'metadata';
    const rejected = document.querySelector('#review-status option[value="rejected"]');
    if (rejected) {
      rejected.disabled = metadata;
      rejected.hidden = metadata;
    }
    const status = document.getElementById('review-status');
    if (metadata && status.value === 'rejected') status.value = 'pending';
  }

  function readFilterInputs(scope) {
    syncStatusOptions();
    var data = new FormData(filter);
    var params = new URLSearchParams();
    data.forEach(function (value, key) {
      if (String(value).trim()) params.set(key, String(value).trim());
    });
    if (scope) params.set('scope', scope);
    if (params.get('scope') === 'metadata' && params.get('status') === 'rejected') {
      params.set('status', 'pending');
    }
    return params.toString();
  }

  function syncScopeControls() {
    document.getElementById('review-scope').value = state.scope;
    Object.keys(scopeLabels).forEach(function (scope) {
      document.getElementById('review-scope-' + scope).setAttribute('aria-pressed', String(scope === state.scope));
    });
    syncStatusOptions();
  }

  function filterParams(cursor, query = state.filterQuery) {
    const params = new URLSearchParams(query);
    if (cursor) params.set('cursor', cursor);
    return params.toString();
  }

  function updateCounters(payload) {
    document.getElementById('count-unresolved').textContent = String(payload.counters.unresolved);
    document.getElementById('review-result-count').textContent = String(payload.total_count);
    const params = new URLSearchParams(state.filterQuery);
    const context = [scopeLabels[state.scope], statusLabels[params.get('status')]];
    if (params.get('q')) context.push('店名「' + params.get('q') + '」');
    document.getElementById('review-result-context').textContent = context.filter(Boolean).join(' / ');
    document.getElementById('review-results').hidden = !params.get('q') && params.get('scope') === 'all' && params.get('status') === 'unresolved';
    empty.textContent = payload.total_count === 0
      ? 'この条件に合う確認項目はありません。確認状況や店名を変更してください。'
      : '一覧から確認する項目を選んでください。';
  }

  function renderQueue() {
    queue.replaceChildren();
    state.items.forEach(function (item, index) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'review-queue__item list-group-item list-group-item-action';
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', index === state.selectedIndex ? 'true' : 'false');
      button.tabIndex = index === state.selectedIndex ? 0 : -1;
      var name = document.createElement('strong');
      const draft = editDrafts.get(state.scope + ':' + item.id);
      name.textContent = (draft && draft.values.shop_name) || (item.shop && item.shop.shop_name) || item.extracted_name;
      var meta = document.createElement('span');
      meta.className = 'small text-body-secondary';
      meta.textContent = draft ? draft.values.area : (item.shop && item.shop.area) || item.extracted_area || '';
      button.append(name, meta);
      button.addEventListener('click', function () { selectItem(index); });
      queue.appendChild(button);
    });
    loadMore.hidden = !state.nextCursor;
    if (state.busy) lockBusyControls();
  }

  function reviewReason(item) {
    const code = effectiveScope(state.items[state.selectedIndex]) === 'metadata' ? item.metadata_difference_type : item.difference_type;
    if (!code) return '確認に回った理由の記録はありません。';
    return reasonLabels.get(code) || '確認に回された項目です。詳細な理由は処理情報で確認できます。';
  }

  function focusSelectedItem() {
    if (state.busy) { focusWhenReady = true; return; }
    if (window.matchMedia('(max-width: 800px)').matches && workspace.classList.contains('review-show-detail')) {
      document.getElementById('review-current-name').focus({preventScroll: true});
      return;
    }
    var selectedItem = queue.querySelector('[role="option"][aria-selected="true"]');
    if (!selectedItem) return;
    selectedItem.focus({ preventScroll: true });
    selectedItem.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  function setText(id, value) {
    var element = document.getElementById(id);
    if (element) element.textContent = value || '—';
  }

  function renderLink(containerId, url, label) {
    var container = document.getElementById(containerId);
    container.replaceChildren();
    if (!url) {
      container.textContent = '—';
      return;
    }
    var link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.className = 'link-secondary link-offset-2';
    link.textContent = label || url;
    container.appendChild(link);
  }

  function renderAssets(item) {
    var container = document.getElementById('review-assets');
    container.replaceChildren();
    item.assets.forEach(function (asset) {
      var card = document.createElement('div');
      card.className = 'review-asset small';
      var link = document.createElement('a');
      link.href = asset.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'link-secondary link-offset-2';
      link.textContent = asset.title || assetLabels.get(asset.kind) || '投稿の添付資料を開く';
      card.appendChild(link);
      if (asset.extracted_text) {
        var assetText = document.createElement('p');
        assetText.className = 'mb-0';
        assetText.textContent = asset.extracted_text;
        card.appendChild(assetText);
      }
      container.appendChild(card);
    });
  }

  function renderCandidates(item) {
    var container = document.getElementById('review-candidates');
    container.replaceChildren();
    candidateButtons.length = 0;
    if (effectiveScope(state.items[state.selectedIndex]) === 'metadata') return;
    if (!item.candidates.length) {
      var emptyCandidate = document.createElement('p');
      emptyCandidate.className = 'text-body-secondary small py-3';
      emptyCandidate.textContent = 'ほかの店舗候補はありません。下の店舗情報を確認し、判断できない場合は「あとで確認」を選んでください。';
      container.appendChild(emptyCandidate);
      return;
    }
    item.candidates.forEach(function (candidate) {
      var card = document.createElement('article');
      card.className = 'review-candidate card p-3';
      if (candidate.is_strong_match) card.classList.add('review-candidate--strong');
      var heading = document.createElement('h3');
      heading.className = 'h6 fw-bold mb-0';
      heading.textContent = candidate.name;
      var facts = document.createElement('p');
      facts.className = 'small mb-0';
      facts.textContent = [candidate.area, candidate.category, candidate.address, candidate.phone]
        .filter(Boolean).join(' / ') || '補足情報なし';
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = '候補の調査情報';
      details.appendChild(summary);
      var score = document.createElement('p');
      score.className = 'review-candidate__score small text-body-secondary mb-0';
      score.textContent = '店名類似度 ' + Math.round(candidate.name_similarity * 100) + '%'
        + (candidate.is_strong_match ? ' · 強い根拠あり' : ' · 手動確認が必要');
      var verification = document.createElement('p');
      verification.className = 'review-candidate__score small text-body-secondary mb-0';
      verification.textContent = (candidate.is_verified ? '検証済み' : '未検証')
        + ' · ' + (provenanceLabels.get(candidate.provenance) || '取得方法の説明なし')
        + '（' + candidate.provenance + '）';
      if (candidate.verification_reason) {
        verification.textContent += ' · ' + candidate.verification_reason;
      }
      details.append(score, verification);
      card.append(heading, facts, details);
      if (candidate.evidence_url) {
        var evidenceLink = document.createElement('a');
        evidenceLink.href = candidate.evidence_url;
        evidenceLink.target = '_blank';
        evidenceLink.rel = 'noopener noreferrer';
        evidenceLink.className = 'link-secondary link-offset-2 small';
        evidenceLink.textContent = '候補の店舗情報を開く ↗';
        card.appendChild(evidenceLink);
      }
      var select = document.createElement('button');
      select.type = 'button';
      select.className = 'btn btn-outline-secondary btn-sm';
      select.textContent = '候補を入力欄で確認';
      select.addEventListener('click', function () {
        if (state.busy || isEventExcluded(item)) return;
        renderSuggestionChoices([{
          shop_name: candidate.name, branch_name: null, area: candidate.area, category: candidate.category,
          address: candidate.address, phone: candidate.phone, canonical_url: candidate.canonical_url,
          evidence_urls: candidate.evidence_url ? [candidate.evidence_url] : [],
          reason: candidate.verification_reason || '既存候補',
        }]);
      });
      candidateButtons.push(select);
      card.appendChild(select);
      container.appendChild(card);
    });
  }

  function rememberEditDraft() {
    const item = state.items[state.selectedIndex];
    if (!item) return;
    const form = document.getElementById('review-edit-form');
    const key = state.scope + ':' + item.id;
    const previous = editDrafts.get(key);
    editDrafts.set(key, {
      values: {
        shop_name: form.elements.shop_name.value,
        branch_name: form.elements.branch_name.value,
        area: form.elements.area.value,
        category: form.elements.category.value,
        address: form.elements.address.value,
        phone: form.elements.phone.value,
        canonical_url: form.elements.canonical_url.value,
      },
      baseValues: previous ? previous.baseValues : savedEditValues(item),
      conflicts: previous ? previous.conflicts : {},
      expectedVersion: previous ? previous.expectedVersion : item.version,
      shopVersion: previous ? previous.shopVersion : (item.shop ? item.shop.version : null),
      shopId: previous ? previous.shopId : (item.shop ? item.shop.id : null),
    });
    if (!hasUnsavedEdits(item)) editDrafts.delete(key);
    invalidateLinkPreview();
    renderEditConflicts(item);
    updateApprovalState();
    updateCurrentName();
    renderQueue();
  }

  function savedEditValues(item) {
    const shop = item.shop || {};
    return {
      shop_name: (item.shop ? shop.shop_name : item.extracted_name === '（店舗名未特定）' ? '' : item.extracted_name) || '',
      branch_name: (item.shop ? shop.branch_name : item.extracted_branch_name) || '',
      area: (item.shop ? shop.area : item.extracted_area) || '',
      category: (item.shop ? shop.category : item.extracted_category) || '',
      address: shop.address || '',
      phone: shop.phone || '',
      canonical_url: shop.canonical_url || '',
    };
  }

  function hasUnsavedEdits(item) {
    const draft = editDrafts.get(state.scope + ':' + item.id);
    if (!draft) return false;
    const saved = savedEditValues(item);
    return Object.keys(saved).some(function (name) { return draft.values[name] !== saved[name]; });
  }

  function hasEditConflicts(item) {
    const draft = editDrafts.get(state.scope + ':' + item.id);
    return Boolean(draft && Object.keys(draft.conflicts).length);
  }

  function rebaseEditDraft(item, scope) {
    const draft = editDrafts.get(scope + ':' + item.id);
    if (!draft) return;
    const latest = savedEditValues(item);
    const shopId = item.shop ? item.shop.id : null;
    Object.keys(latest).forEach(function (name) {
      const changed = draft.values[name] !== draft.baseValues[name];
      if (draft.values[name] === latest[name]) {
        delete draft.conflicts[name];
      } else if (draft.conflicts[name] !== undefined || (changed && (latest[name] !== draft.baseValues[name] || shopId !== draft.shopId))) {
        draft.conflicts[name] = latest[name];
      } else if (!changed) {
        draft.values[name] = latest[name];
      }
    });
    draft.baseValues = latest;
    draft.expectedVersion = item.version;
    draft.shopVersion = item.shop ? item.shop.version : null;
    draft.shopId = shopId;
  }

  function renderEditConflicts(item) {
    const container = document.getElementById('review-edit-conflicts');
    container.replaceChildren();
    const draft = editDrafts.get(state.scope + ':' + item.id);
    container.hidden = !hasEditConflicts(item);
    if (container.hidden) return;
    const heading = document.createElement('p');
    heading.textContent = '同じ項目が更新されています。入力内容と最新の保存値を比べ、項目ごとに残す値を選んでください。選択後に保存できます。';
    container.appendChild(heading);
    Object.keys(draft.conflicts).forEach(function (name) {
      const row = document.createElement('div');
      row.className = 'mb-2 text-break';
      const values = document.createElement('p');
      values.textContent = fieldLabels[name] + ' — 入力: ' + (draft.values[name] || '（空欄）')
        + ' / 最新: ' + (draft.conflicts[name] || '（空欄）');
      row.appendChild(values);
      [false, true].forEach(function (useLatest) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn btn-outline-secondary btn-sm me-2';
        button.textContent = fieldLabels[name] + (useLatest ? '：最新の値を使う' : '：入力を残す');
        button.addEventListener('click', function () {
          if (state.busy) return;
          if (useLatest) draft.values[name] = draft.conflicts[name];
          delete draft.conflicts[name];
          fillEditForm(item);
        });
        row.appendChild(button);
      });
      container.appendChild(row);
    });
  }

  function syncSharedShop(shop) {
    if (!shop) return;
    state.items.forEach(function (item) {
      if (!item.shop || item.shop.id !== shop.id) return;
      item.shop = Object.assign({}, item.shop, shop);
      ['all', 'identity', 'metadata'].forEach(function (scope) { rebaseEditDraft(item, scope); });
    });
  }

  function fillEditForm(item) {
    var form = document.getElementById('review-edit-form');
    const draft = editDrafts.get(state.scope + ':' + item.id);
    const values = draft ? draft.values : savedEditValues(item);
    Object.keys(values).forEach(function (name) { form.elements[name].value = values[name]; });
    renderEditConflicts(item);
    updateApprovalState();
  }

  function renderItem() {
    var item = state.items[state.selectedIndex];
    invalidateLinkPreview();
    const nextLinkKey = item ? state.scope + ':' + item.id : null;
    if (nextLinkKey !== linkItemKey) {
      linkForm.elements.target_shop_id.value = '';
      linkForm.elements.note.value = '';
      linkItemKey = nextLinkKey;
    }
    document.getElementById('review-link-section').hidden = !item || effectiveScope(state.items[state.selectedIndex]) !== 'identity';
    if (!item) {
      evidence.hidden = true;
      decisionPanel.hidden = true;
      empty.hidden = false;
      updateApprovalState();
      return;
    }
    document.getElementById('review-issues').textContent = (item.issues || []).map(function (issue) { return issue.label; }).join(' / ') || '登録内容と元投稿を確認してください。';
    const extra = supplementDrafts.get(state.scope + ':' + item.id) || { supplement: '', reference_url: '' };
    document.getElementById('review-supplement').value = extra.supplement;
    document.getElementById('review-reference-url').value = extra.reference_url;
    document.getElementById('review-ai-results').replaceChildren();
    empty.hidden = true;
    evidence.hidden = false;
    decisionPanel.hidden = false;
    setText('review-message-id', item.message_id);
    var discordLink = document.getElementById('review-discord-link');
    discordLink.hidden = !item.discord_url;
    if (item.discord_url) discordLink.href = item.discord_url;
    setText('review-message-content', item.message_content || '投稿本文を取得できません。');
    const sourceWarning = document.getElementById('review-source-warning');
    sourceWarning.hidden = !item.fetch_error;
    sourceWarning.textContent = '元の投稿を取得できていません。表示されている情報で判断できない場合は、あとで確認してください。';
    setText('review-extracted-name', item.extracted_name);
    setText('review-extracted-branch', item.extracted_branch_name);
    setText('review-extracted-area', item.extracted_area);
    setText('review-extracted-category', item.extracted_category);
    renderLink('review-source-url', item.source_url, '投稿元を開く ↗');
    setText(
      'review-difference-code',
      effectiveScope(state.items[state.selectedIndex]) === 'metadata' ? item.metadata_difference_type : item.difference_type
    );
    setText('review-difference', reviewReason(item));
    setText('review-extraction-source', item.extraction_source);
    setText('review-confidence', item.confidence_reason);
    var groupRow = document.getElementById('review-group-row');
    if (item.review_group_count > 1) {
      groupRow.hidden = false;
      setText(
        'review-group',
        '同じ店と思われる項目が ' + item.review_group_count + '件あります。確認結果によって、ほかの未確認項目も確認済みになることがあります。'
      );
    } else {
      groupRow.hidden = true;
      setText('review-group', null);
    }
    setText('review-group-reason', item.review_group_reason);
    setText('review-extraction-error', item.extraction_error || item.fetch_error);
    document.querySelector('.review-merge').hidden = effectiveScope(state.items[state.selectedIndex]) === 'metadata';
    document.getElementById('review-reject').hidden = false;
    document.getElementById('review-edit-form').elements.shop_name.readOnly = effectiveScope(state.items[state.selectedIndex]) === 'metadata';
    document.getElementById('review-edit-form').elements.branch_name.readOnly = effectiveScope(state.items[state.selectedIndex]) === 'metadata';
    document.getElementById('review-edit-form').elements.canonical_url.readOnly = effectiveScope(state.items[state.selectedIndex]) === 'metadata';
    setText('review-edit-scope-hint', effectiveScope(state.items[state.selectedIndex]) === 'metadata'
      ? 'エリア・カテゴリを確認します。住所と電話番号も修正できます。店名・支店名・店舗URLを直す場合は「店・支店を確認」に切り替えてください。'
      : '元の投稿と店名・支店名を見比べてください。修正すると、同じ店に登録されている店舗情報も更新されます。');
    renderPhoto(item);
    renderAssets(item);
    renderCandidates(item);
    fillEditForm(item);
    updateCurrentName();
    renderQueue();
  }

  function selectItem(index) {
    if (state.busy || index < 0 || index >= state.items.length) return;
    state.selectedIndex = index;
    workspace.classList.add('review-show-detail');
    renderItem();
    focusSelectedItem();
  }

  function loadQueue(reset, trigger, query = state.filterQuery) {
    if (state.busy) return Promise.resolve();
    invalidateLinkPreview();
    const operation = setBusy(true, trigger || filterSubmit, '読み込み中…');
    clearError();
    var cursor = reset ? null : state.nextCursor;
    return apiJson('/api/admin/reviews?' + filterParams(cursor, query), { method: 'GET' })
      .then(function (payload) {
        state.filterQuery = query;
        state.scope = payload.scope;
        syncScopeControls();
        updateCounters(payload);
        state.items = reset ? payload.items : state.items.concat(payload.items);
        state.nextCursor = payload.next_cursor;
        if (reset) state.selectedIndex = state.items.length ? 0 : -1;
        const requestedId = Number(new URLSearchParams(window.location.search).get('mention_id'));
        if (reset && requestedId && !state.openedRequestedItem) {
          state.openedRequestedItem = true;
          workspace.classList.add('review-show-detail');
          const index = state.items.findIndex(function (item) { return item.id === requestedId; });
          if (index >= 0) state.selectedIndex = index;
          else return apiJson('/api/admin/reviews/' + requestedId, {method: 'GET'}).then(function (item) { state.items.unshift(item); state.selectedIndex = 0; renderQueue(); renderItem(); });
        }
        renderQueue();
        renderItem();
      })
      .catch(function (error) {
        console.error('Review queue load failed', { reset: reset, error: error });
        setError('確認する項目を読み込めませんでした。入力内容は保持されています: ' + error.message, function () {
          loadQueue(reset, retryButton, query);
        });
      })
      .finally(function () { setBusy(false, null, '', operation); })
      .then(function () {
        if (!trigger || !errorBox.hidden) return;
        if (state.selectedIndex >= 0) focusSelectedItem();
        else { workspace.classList.remove('review-show-detail'); document.getElementById('review-query').focus({ preventScroll: true }); }
      });
  }

  function submitDecision(payload, trigger) {
    var item = state.items[state.selectedIndex];
    if (!item || state.busy) return;
    rememberEditDraft();
    if (payload.action === 'edit_and_approve' && (hasEditConflicts(item) || (isEventExcluded(item) && !hasUnsavedEdits(item)))) return;
    if (payload.action === 'approve_current' && !canApproveCurrent(item)) return;
    if (payload.action === 'approve_candidate' && (hasUnsavedEdits(item) || isEventExcluded(item))) return;
    const draft = editDrafts.get(state.scope + ':' + item.id);
    payload.expected_version = draft ? draft.expectedVersion : item.version;
    payload.scope = payload.action === 'exclude' ? 'identity' : effectiveScope(item);
    if (draft) {
      payload.shop_version = draft.shopVersion;
    } else if (item.shop) {
      payload.shop_version = item.shop.version;
    }
    sendDecision(item, JSON.stringify(payload), state.scope, trigger);
  }

  function refreshCounters() {
    const operation = setBusy(true, retryButton.hidden ? null : retryButton, '更新中…');
    clearError();
    return apiJson('/api/admin/reviews?' + filterParams(null), { method: 'GET' })
      .then(updateCounters)
      .catch(function (error) {
        console.error('Review counters refresh failed after saving', error);
        setError('保存は完了しましたが、件数を更新できませんでした: ' + error.message, refreshCounters);
      })
      .finally(function () { setBusy(false, null, '', operation); })
      .then(function () { if (errorBox.hidden) focusSelectedItem(); });
  }

  function refreshConflict(item, requestScope) {
    if (state.busy) return;
    invalidateLinkPreview();
    const operation = setBusy(true, retryButton, '読み込み中…');
    clearError();
    return apiJson('/api/admin/reviews/' + item.id, { method: 'GET' })
      .then(function (latest) {
        const index = state.items.findIndex(function (queuedItem) { return queuedItem.id === item.id; });
        if (index < 0) throw new Error('対象の確認項目が一覧にありません。');
        state.items[index] = latest;
        rebaseEditDraft(latest, requestScope);
        syncSharedShop(latest.shop);
        state.selectedIndex = index;
        renderItem();
        showNotice('最新の情報を読み込みました。入力した変更は保持し、変更していない項目には最新値を反映しました。確認状態: '
          + (statusLabels[requestScope === 'metadata' ? latest.metadata_review_status : latest.review_status] || '不明')
          + '。内容を確認してから保存してください。');
      })
      .catch(function (error) {
        console.error('Review conflict refresh failed', { mentionId: item.id, error: error });
        setError('最新の情報を読み込めませんでした。入力内容は保持されています: ' + error.message, function () {
          refreshConflict(item, requestScope);
        }, '最新情報を読み込む');
      })
      .finally(function () { setBusy(false, null, '', operation); });
  }

  function sendDecision(item, requestBody, requestScope, trigger, endpoint = 'decision', linkRequestGeneration = null) {
    if (state.busy) return;
    if (endpoint === 'link' && (linkRequestGeneration !== linkGeneration
      || effectiveScope(state.items[state.selectedIndex]) !== 'identity' || state.items[state.selectedIndex]?.id !== item.id)) {
      setError('対象が変わっています。関連付けの内容をもう一度確認してください。');
      return;
    }
    if (endpoint !== 'link') invalidateLinkPreview();
    const operation = setBusy(true, trigger, '保存中…');
    clearError();
    clearNotice();
    apiJson('/api/admin/reviews/' + item.id + '/' + endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: requestBody,
    })
      .then(function (result) {
        var automaticallyResolvedIds = result.automatically_resolved_mention_ids || [];
        var removedIds = new Set([item.id].concat(automaticallyResolvedIds));
        removedIds.forEach(function (id) {
          editDrafts.delete(requestScope + ':' + id);
          selectedSuggestions.delete(requestScope + ':' + id);
          supplementDrafts.delete(requestScope + ':' + id);
          photoDrafts.delete(id);
          photoPreviews.delete(id);
          initializedPhotos.delete(id);
          failedPhotos.forEach(function (key) { if (key.startsWith(id + ':')) failedPhotos.delete(key); });
        });
        var nextItemId = nextRemainingItemId(
          state.items,
          state.selectedIndex,
          removedIds
        );
        state.items = state.items.filter(function (queuedItem) {
          return !removedIds.has(queuedItem.id);
        });
        syncSharedShop(result.source_shop);
        syncSharedShop(result.shop);
        state.items.forEach(function (queuedItem) {
          queuedItem.review_group_mention_ids = queuedItem.review_group_mention_ids.filter(
            function (mentionId) { return !removedIds.has(mentionId); }
          );
          queuedItem.review_group_count = queuedItem.review_group_mention_ids.length;
        });
        if (result.automatically_resolved_count > 0) {
          showNotice(
            '確認結果を保存しました。同じ店の未確認項目 '
            + result.automatically_resolved_count
            + '件も確認済みになりました。'
          );
        } else {
          showNotice('確認結果を保存しました。');
        }
        state.selectedIndex = nextItemId === null
          ? -1
          : state.items.findIndex(function (queuedItem) {
            return queuedItem.id === nextItemId;
          });
        if (requestScope === 'all' || !state.items.length) {
          setBusy(false, null, '', operation);
          return loadQueue(true).then(function () {
            if (!errorBox.hidden) return;
            if (state.selectedIndex >= 0) focusSelectedItem();
            else { workspace.classList.remove('review-show-detail'); document.getElementById('review-query').focus({ preventScroll: true }); }
          });
        }
        renderQueue();
        renderItem();
        return refreshCounters();
      })
      .catch(function (error) {
        console.error('Review decision save failed', { mentionId: item.id, error: error });
        if (error instanceof ReviewConflictError) {
          if (endpoint === 'link') invalidateLinkPreview();
          setError('別の更新があり、保存できませんでした。入力内容は保持されています。「最新情報を読み込む」で変更を比較できます。', function () {
            refreshConflict(item, requestScope);
          }, '最新情報を読み込む');
          return;
        }
        if (error instanceof ReviewDecisionError) {
          if (endpoint === 'link') invalidateLinkPreview();
          setError('保存できませんでした。入力内容は保持されています: ' + error.message);
          return;
        }
        setError('データ確認結果を保存できませんでした。入力内容は保持されています: ' + error.message, function () {
          if (endpoint === 'decision') {
            const decision = JSON.parse(requestBody);
            if (decision.action === 'edit_and_approve' && state.scope === requestScope && state.items[state.selectedIndex]?.id === item.id) {
              const latest = readRegistration();
              if (['shop', 'supplement', 'photo_asset_id'].some(function (key) { return JSON.stringify(latest[key]) !== JSON.stringify(decision[key]); })) {
                if (editSubmit.disabled) { setError('入力内容を確認してから登録してください。入力は保持しています。'); return; }
                submitDecision(latest, retryButton);
                return;
              }
            }
          }
          sendDecision(item, requestBody, requestScope, retryButton, endpoint, linkRequestGeneration);
        });
      })
      .finally(function () { setBusy(false, null, '', operation); });
  }

  function hasLinkDraft(item) {
    if (!item) return false;
    const saved = savedEditValues(item);
    return ['all', 'identity', 'metadata'].some(function (scope) {
      const draft = editDrafts.get(scope + ':' + item.id);
      return draft && (Object.keys(draft.conflicts).length
        || Object.keys(saved).some(function (name) { return draft.values[name] !== saved[name]; }));
    });
  }

  function linkInputsValid() {
    const target = Number(linkForm.elements.target_shop_id.value);
    const note = linkForm.elements.note.value.trim();
    return Number.isSafeInteger(target) && target > 0 && note.length > 0 && note.length <= 2000;
  }

  function updateLinkControls() {
    const item = state.items[state.selectedIndex];
    const draft = hasLinkDraft(item);
    const blocked = state.busy || workspace.dataset.readOnly === 'true' || linkLoading || !item || effectiveScope(state.items[state.selectedIndex]) !== 'identity' || draft;
    linkPreviewButton.disabled = blocked || !linkInputsValid();
    linkApplyButton.disabled = blocked || !linkPreview || !linkConfirm.checked;
    document.getElementById('review-link-draft-hint').hidden = !draft;
  }

  function invalidateLinkPreview() {
    linkGeneration += 1;
    linkPreview = null;
    linkLoading = false;
    linkConfirm.checked = false;
    document.getElementById('review-link-preview').hidden = true;
    updateLinkControls();
  }

  function renderLinkShop(containerId, shop) {
    const container = document.getElementById(containerId);
    container.replaceChildren();
    const identity = document.createElement('p');
    identity.textContent = shop
      ? 'ID ' + shop.id + ' / ' + shop.shop_name + ' / 支店: ' + (shop.branch_name || 'なし') + ' / エリア: ' + (shop.area || '未設定')
      : '現在は店舗に関連付いていません。';
    container.appendChild(identity);
    if (!shop) return;
    const retained = document.createElement('p');
    retained.textContent = '保持する店舗情報 — 訪問: ' + (shop.is_visited ? '訪問済み' : '未訪問')
      + ' / 訪問日: ' + (shop.visited_at || '未設定') + ' / 評価: ' + (shop.rating ?? '未設定')
      + ' / メモ: ' + (shop.memo || 'なし') + ' / カテゴリ: ' + (shop.category || '未設定')
      + ' / 住所: ' + (shop.address || '未設定') + ' / 電話: ' + (shop.phone || '未設定')
      + ' / 店舗URL: ' + (shop.canonical_url || '未設定');
    container.appendChild(retained);
  }

  function previewSingleLink() {
    const item = state.items[state.selectedIndex];
    if (!item || state.busy || workspace.dataset.readOnly === 'true' || linkLoading || effectiveScope(state.items[state.selectedIndex]) !== 'identity') return;
    rememberEditDraft();
    if (hasLinkDraft(item)) {
      setError('店舗情報を編集中です。入力を保存するか元の値に戻してから関連付けてください。入力は保持しています。');
      return;
    }
    if (!linkInputsValid()) {
      setError('正しい店舗IDと、1〜2000文字の判断根拠を入力してください。');
      return;
    }
    const targetId = Number(linkForm.elements.target_shop_id.value);
    const generation = linkGeneration;
    const query = readFilterInputs();
    linkLoading = true;
    updateLinkControls();
    clearError();
    const current = function () {
      return generation === linkGeneration && effectiveScope(state.items[state.selectedIndex]) === 'identity'
        && state.items[state.selectedIndex]?.id === item.id
        && Number(linkForm.elements.target_shop_id.value) === targetId && readFilterInputs() === query;
    };
    apiJson('/api/admin/reviews/' + item.id + '/link-preview?target_shop_id=' + targetId, { method: 'GET' })
      .then(function (preview) {
        if (!current()) return;
        const validShop = function (shop) {
          return shop && Number.isSafeInteger(shop.id) && shop.id > 0
            && Number.isSafeInteger(shop.version) && shop.version > 0 && typeof shop.shop_name === 'string';
        };
        if (preview.mention_id !== item.id || !Number.isSafeInteger(preview.expected_version) || preview.expected_version < 1
          || !validShop(preview.target_shop) || preview.target_shop.id !== targetId
          || (preview.source_shop !== null && !validShop(preview.source_shop))
          || !Number.isSafeInteger(preview.source_mention_count) || preview.source_mention_count < 0
          || !Number.isSafeInteger(preview.target_mention_count) || preview.target_mention_count < 0
          || typeof preview.source_will_be_empty !== 'boolean') {
          throw new Error('関連付けの確認内容が対象と一致しません。もう一度確認してください。');
        }
        const displayed = state.items[state.selectedIndex];
        const sameSource = preview.source_shop === null
          ? !displayed.shop
          : displayed.shop && preview.source_shop.id === displayed.shop.id && preview.source_shop.version === displayed.shop.version;
        if (preview.expected_version !== displayed.version || !sameSource) {
          throw new ReviewConflictError('表示中の投稿・店舗情報が更新されています。最新情報を読み込んでから再確認してください。');
        }
        linkPreview = { data: preview, generation: generation, query: query };
        renderLinkShop('review-link-source', preview.source_shop);
        renderLinkShop('review-link-target-preview', preview.target_shop);
        setText('review-link-counts', '変更する確認項目: 1件 / 現在の関連件数 — 元の店舗: '
          + preview.source_mention_count + '件 / 関連付け先: ' + preview.target_mention_count + '件');
        setText('review-link-status', '店・支店: ' + (statusLabels[preview.review_status] || '不明')
          + ' → 確認済み / エリア・カテゴリ: ' + (statusLabels[preview.metadata_review_status] || '不明') + '（変更しません）');
        document.getElementById('review-link-empty-warning').hidden = !preview.source_will_be_empty;
        document.getElementById('review-link-preview').hidden = false;
      })
      .catch(function (error) {
        if (!current()) return;
        console.error('Single mention link preview failed', { mentionId: item.id, targetId: targetId, error: error });
        invalidateLinkPreview();
        if (error instanceof ReviewConflictError) {
          setError(error.message + ' 入力は保持しています。', function () {
            refreshConflict(item, 'identity');
          }, '最新情報を読み込む');
          return;
        }
        setError('関連付けの内容を確認できませんでした: ' + error.message);
      })
      .finally(function () {
        if (generation === linkGeneration) linkLoading = false;
        updateLinkControls();
      });
  }

  linkForm.addEventListener('submit', function (event) {
    event.preventDefault();
    previewSingleLink();
  });
  [linkForm.elements.target_shop_id, linkForm.elements.note].forEach(function (control) {
    control.addEventListener('input', invalidateLinkPreview);
    control.addEventListener('change', invalidateLinkPreview);
  });
  linkConfirm.addEventListener('change', updateLinkControls);
  linkApplyButton.addEventListener('click', function () {
    const item = state.items[state.selectedIndex];
    const confirmed = linkPreview;
    if (!item || state.busy || !confirmed || !linkConfirm.checked || effectiveScope(state.items[state.selectedIndex]) !== 'identity') return;
    const preview = confirmed.data;
    if (hasLinkDraft(item) || !linkInputsValid() || confirmed.generation !== linkGeneration
      || preview.mention_id !== item.id || preview.target_shop.id !== Number(linkForm.elements.target_shop_id.value)
      || confirmed.query !== readFilterInputs()) {
      invalidateLinkPreview();
      setError('入力または対象が変わっています。関連付けの内容をもう一度確認してください。入力は保持しています。');
      return;
    }
    const requestBody = JSON.stringify({
      expected_version: preview.expected_version,
      source_shop_id: preview.source_shop ? preview.source_shop.id : null,
      source_shop_version: preview.source_shop ? preview.source_shop.version : null,
      target_shop_id: preview.target_shop.id, target_shop_version: preview.target_shop.version,
      note: linkForm.elements.note.value.trim(),
    });
    linkPreview = null;
    linkConfirm.checked = false;
    sendDecision(item, requestBody, 'identity', linkApplyButton, 'link', linkGeneration);
  });
  filter.addEventListener('input', invalidateLinkPreview);
  filter.addEventListener('change', invalidateLinkPreview);

  filter.addEventListener('submit', function (event) {
    event.preventDefault();
    if (state.busy) return;
    document.querySelector('.review-filter-menu').removeAttribute('open');
    loadQueue(true, filterSubmit, readFilterInputs());
  });
  retryButton.addEventListener('click', function () {
    if (retryAction && !state.busy) retryAction();
  });
  loadMore.addEventListener('click', function () { loadQueue(false, loadMore); });
  document.getElementById('review-reject').addEventListener('click', function () {
    const item = state.items[state.selectedIndex];
    if (state.busy || workspace.dataset.readOnly === 'true' || !item) return;
    const message = '確認項目「' + item.extracted_name + '」を確認対象から削除します。元投稿・店舗情報・操作履歴は残ります。'
      + (hasUnsavedEdits(item) ? '\n編集中の変更は保存されません。' : '');
    if (!window.confirm(message)) return;
    submitDecision({ action: 'exclude' }, this);
  });

  function readRegistration() {
    const data = new FormData(document.getElementById('review-edit-form'));
    return {
      action: 'edit_and_approve',
      confirm_metadata: state.scope === 'all',
      photo_asset_id: photoDrafts.get(state.items[state.selectedIndex].id) || null,
      supplement: document.getElementById('review-supplement').value || null,
      suggestion_evidence_urls: (selectedSuggestions.get(state.scope + ':' + state.items[state.selectedIndex].id) || {}).evidence_urls || [],
      suggestion_reason: (selectedSuggestions.get(state.scope + ':' + state.items[state.selectedIndex].id) || {}).reason || null,
      shop: {
        shop_name: String(data.get('shop_name') || ''),
        branch_name: String(data.get('branch_name') || '') || null,
        area: String(data.get('area') || ''),
        category: String(data.get('category') || ''),
        address: String(data.get('address') || '') || null,
        phone: String(data.get('phone') || '') || null,
        canonical_url: String(data.get('canonical_url') || '') || null,
      },
    };
  }

  document.getElementById('review-edit-form').addEventListener('input', function (event) {
    if (event.target === document.getElementById('review-photo-select')) return;
    rememberEditDraft();
  });
  document.getElementById('review-edit-form').addEventListener('submit', function (event) {
    event.preventDefault();
    if (state.busy || !state.items[state.selectedIndex] || editSubmit.disabled) return;
    submitDecision(readRegistration(), editSubmit);
  });

  var mergeForm = document.getElementById('review-merge-form');
  mergeForm.elements.target_shop_id.addEventListener('change', function () {
    if (state.busy) return;
    var targetId = this.value;
    mergeForm.elements.target_version.value = '';
    if (!targetId) return;
    const operation = setBusy(true, mergeSubmit, '確認中…');
    clearError();
    apiJson('/api/admin/shops/' + targetId + '/merge-preview', { method: 'GET' })
      .then(function (shop) {
        mergeForm.elements.target_version.value = shop.version;
        mergeForm.elements.is_visited.checked = shop.is_visited;
        mergeForm.elements.visited_at.value = shop.visited_at ? shop.visited_at.slice(0, 10) : '';
        mergeForm.elements.rating.value = shop.rating || '';
        mergeForm.elements.memo.value = shop.memo || '';
        document.getElementById('merge-preview').textContent = 'まとめ先: '
          + [shop.shop_name, shop.branch_name, shop.area || 'エリア不明'].filter(Boolean).join(' / ');
      })
      .catch(function (error) { setError('統合先を確認できませんでした: ' + error.message); })
      .finally(function () { setBusy(false, null, '', operation); });
  });
  mergeForm.addEventListener('submit', function (event) {
    event.preventDefault();
    if (state.busy || workspace.dataset.readOnly === 'true') return;
    var data = new FormData(mergeForm);
    if (!data.get('target_version')) {
      setError('統合先店舗を確認してから実行してください。');
      return;
    }
    const item = state.items[state.selectedIndex];
    if (!item) return;
    const mergeMessage = item.shop
      ? '元店舗「' + item.shop.shop_name + '」のすべての確認項目を統合先へ移し、元の店舗を削除します。'
      : 'この確認項目を統合先の店舗へ関連付けます。店舗は削除しません。';
    if (!window.confirm(mergeMessage
      + '\n統合先の訪問状況・訪問日・評価・メモは入力内容で更新します。元投稿・添付・確認履歴は残ります。'
      + '\n画面から元に戻す機能はありません。続けますか？')) return;
    submitDecision({
      action: 'merge',
      target_shop_id: Number(data.get('target_shop_id')),
      target_version: Number(data.get('target_version')),
      is_visited: data.get('is_visited') === 'on',
      visited_at: data.get('visited_at') ? String(data.get('visited_at')) + 'T00:00:00Z' : null,
      rating: data.get('rating') ? Number(data.get('rating')) : null,
      memo: String(data.get('memo') || '') || null,
    }, mergeSubmit);
  });

  function isShortcutScopeActive(target) {
    return target instanceof Element
      && (target === workspace || target === queue || queue.contains(target));
  }

  function isShortcutBlockedTarget(target) {
    if (!(target instanceof Element)) return true;
    return Boolean(target.closest(
      'input, textarea, select, button:not([role="option"]), a, summary, [contenteditable="true"]'
    ));
  }

  document.addEventListener('keydown', function (event) {
    if (
      state.busy
      || event.altKey
      || event.ctrlKey
      || event.metaKey
      || event.shiftKey
      || !isShortcutScopeActive(event.target)
      || isShortcutBlockedTarget(event.target)
    ) return;

    var key = event.key.toLowerCase();
    var nextIndex = null;
    if (key === 'j' || event.key === 'ArrowDown') {
      nextIndex = Math.min(state.selectedIndex + 1, state.items.length - 1);
    } else if (key === 'k' || event.key === 'ArrowUp') {
      nextIndex = Math.max(state.selectedIndex - 1, 0);
    } else if (event.key === 'Home') {
      nextIndex = 0;
    } else if (event.key === 'End') {
      nextIndex = state.items.length - 1;
    }
    if (nextIndex !== null && state.items.length) {
      event.preventDefault();
      selectItem(nextIndex);
      return;
    }

    if (event.repeat) return;
    if (key === 'a' && !editSubmit.disabled) {
      event.preventDefault();
      editSubmit.click();
    } else if (key === 'x') {
      event.preventDefault();
      document.getElementById('review-reject').click();
    }
  });

  document.getElementById('review-scope').addEventListener('change', syncStatusOptions);
  Object.keys(scopeLabels).forEach(function (scope) {
    document.getElementById('review-scope-' + scope).addEventListener('click', function () {
      if (state.busy || scope === state.scope) return;
      loadQueue(true, filterSubmit, readFilterInputs(scope));
    });
  });

  const supplementDrafts = new Map();
  const selectedSuggestions = new Map();
  const supplementInput = document.getElementById('review-supplement');
  const referenceInput = document.getElementById('review-reference-url');
  const aiButton = document.getElementById('review-ai-search');

  function rememberSupplement() {
    const item = state.items[state.selectedIndex];
    if (!item) return;
    supplementDrafts.set(state.scope + ':' + item.id, {
      supplement: supplementInput.value, reference_url: referenceInput.value,
    });
  }
  supplementInput.addEventListener('input', rememberSupplement);
  referenceInput.addEventListener('input', rememberSupplement);

  /** @typedef {'shop_name'|'branch_name'|'area'|'category'} ComplementField */
  /** @typedef {{shop_name: string, branch_name: string|null, area: string|null, category: string|null, evidence_urls: string[], reason: string}} Suggestion */
  /** @typedef {{assetId: number|null, url: string|null}} PhotoOption */
  /** @typedef {{id: number, shop: {image_key?: string|null}|null, assets: {id: number, photo_url?: string|null}[]}} PhotoReviewItem */
  /** @type {ComplementField[]} */
  const complementFields = ['shop_name', 'branch_name', 'area', 'category'];
  /** @type {Map<number, number|null>} */
  const photoDrafts = new Map();
  /** @type {Map<number, number|null>} */
  const photoPreviews = new Map();
  /** @type {Set<number>} */
  const initializedPhotos = new Set();
  /** @type {Set<string>} */
  const failedPhotos = new Set();

  /** @returns {ComplementField[]} */
  function emptyComplementFields() {
    const form = document.getElementById('review-edit-form');
    return complementFields.filter(function (name) { return !form.elements[name].readOnly && !form.elements[name].value.trim(); });
  }

  function updateCurrentName() {
    const item = state.items[state.selectedIndex];
    if (!item) return;
    const input = document.getElementById('review-edit-form').elements.shop_name;
    document.getElementById('review-current-name').textContent = input.value.trim() || item.extracted_name;
  }

  /** @param {PhotoReviewItem} item @returns {PhotoOption[]} */
  function photoOptions(item) {
    const existing = item.shop && item.shop.image_key;
    const posted = item.assets.filter(function (asset) { return asset.photo_url; }).map(function (asset) { return {assetId: asset.id, url: asset.photo_url}; });
    if (existing) return [{assetId: null, url: '/media/shop-uploads/' + existing + '.webp'}].concat(posted);
    return posted.length ? posted : [{assetId: null, url: null}];
  }

  /** @param {number} itemId @param {number|null} assetId @returns {string} */
  function photoKey(itemId, assetId) {
    return itemId + ':' + (assetId === null ? 'current' : assetId);
  }

  /** @param {PhotoReviewItem|undefined} item @returns {boolean} */
  function validPhotoSelection(item) {
    if (!item) return true;
    const photos = photoOptions(item).filter(function (choice) { return choice.url; });
    if (!photos.length) return true;
    const selectedId = photoDrafts.get(item.id);
    const selected = photos.find(function (choice) { return choice.assetId === selectedId; });
    return Boolean(selected && photoDrafts.has(item.id) && !failedPhotos.has(photoKey(item.id, selected.assetId)));
  }

  /** @param {PhotoReviewItem|undefined} item @returns {void} */
  function renderPhotoSelection(item) {
    const control = document.getElementById('review-photo-select');
    const label = document.getElementById('review-photo-selection');
    const status = document.getElementById('review-photo-selection-status');
    const photos = item ? photoOptions(item).filter(function (choice) { return choice.url; }) : [];
    const previewId = item ? photoPreviews.get(item.id) ?? null : null;
    const preview = photos.find(function (choice) { return choice.assetId === previewId; });
    const selectedId = item ? photoDrafts.get(item.id) : undefined;
    const selected = photos.find(function (choice) { return choice.assetId === selectedId; });
    label.hidden = !preview;
    control.checked = Boolean(preview && selected && preview.assetId === selected.assetId);
    control.disabled = state.busy || workspace.dataset.readOnly === 'true' || !preview
      || failedPhotos.has(photoKey(item.id, preview.assetId));
    status.textContent = !photos.length ? '' : !selected ? '登録する写真を1枚選んでください。'
      : failedPhotos.has(photoKey(item.id, selected.assetId)) ? '選んだ写真を取得できません。再試行するか選び直してください。'
        : !control.checked ? '登録用：' + (photos.indexOf(selected) + 1) + '枚目' : '';
    status.hidden = !status.textContent;
  }

  /** @param {PhotoReviewItem} item @returns {void} */
  function renderPhoto(item) {
    const choices = photoOptions(item);
    if (!initializedPhotos.has(item.id)) {
      initializedPhotos.add(item.id);
      if (item.shop?.image_key || (choices.length === 1 && choices[0].url)) photoDrafts.set(item.id, choices[0].assetId);
    }
    if (!photoPreviews.has(item.id)) photoPreviews.set(item.id, choices[0].assetId);
    const previewId = photoPreviews.get(item.id) ?? null;
    const preview = choices.find(function (choice) { return choice.assetId === previewId; });
    const link = document.getElementById('review-photo-link');
    const image = document.createElement('img');
    image.id = 'review-photo'; image.alt = '店舗写真の候補';
    link.replaceChildren(image);
    link.hidden = !preview || !preview.url;
    document.getElementById('review-photo-empty').hidden = !link.hidden;
    const failed = Boolean(preview && failedPhotos.has(photoKey(item.id, preview.assetId)));
    document.getElementById('review-photo-error').hidden = !failed;
    document.getElementById('review-photo-retry').hidden = !failed;
    const photos = choices.filter(function (choice) { return choice.url; });
    document.getElementById('review-photo-position').textContent = preview && preview.url ? (photos.indexOf(preview) + 1) + ' / ' + photos.length : '';
    ['previous', 'next'].forEach(function (direction) { document.getElementById('review-photo-' + direction).hidden = photos.length < 2; });
    if (preview && preview.url) {
      link.href = preview.url;
      image.onerror = function () {
        if (state.items[state.selectedIndex]?.id !== item.id || photoPreviews.get(item.id) !== previewId || link.firstElementChild !== image) return;
        failedPhotos.add(photoKey(item.id, previewId));
        document.getElementById('review-photo-error').hidden = false;
        document.getElementById('review-photo-retry').hidden = false;
        updateApprovalState();
      };
      image.onload = function () {
        if (state.items[state.selectedIndex]?.id !== item.id || photoPreviews.get(item.id) !== previewId || link.firstElementChild !== image) return;
        failedPhotos.delete(photoKey(item.id, previewId));
        document.getElementById('review-photo-error').hidden = true;
        document.getElementById('review-photo-retry').hidden = true;
        updateApprovalState();
      };
      image.src = preview.url;
    }
    updateApprovalState();
  }

  /** @param {number} step @returns {void} */
  function movePhoto(step) {
    const item = state.items[state.selectedIndex];
    if (!item || state.busy) return;
    const choices = photoOptions(item);
    const index = choices.findIndex(function (choice) { return choice.assetId === photoPreviews.get(item.id); });
    photoPreviews.set(item.id, choices[(index + step + choices.length) % choices.length].assetId);
    renderPhoto(item);
  }
  document.getElementById('review-photo-select').addEventListener('change', function () {
    const item = state.items[state.selectedIndex];
    if (!item || state.busy || workspace.dataset.readOnly === 'true') return;
    const previewId = photoPreviews.get(item.id) ?? null;
    const preview = photoOptions(item).find(function (choice) { return choice.assetId === previewId; });
    if (!preview?.url || failedPhotos.has(photoKey(item.id, previewId))) return;
    if (this.checked) photoDrafts.set(item.id, previewId);
    else if (photoDrafts.get(item.id) === previewId) photoDrafts.delete(item.id);
    updateApprovalState();
  });
  document.getElementById('review-photo-previous').addEventListener('click', function () { movePhoto(-1); });
  document.getElementById('review-photo-next').addEventListener('click', function () { movePhoto(1); });
  document.getElementById('review-photo-retry').addEventListener('click', function () {
    const item = state.items[state.selectedIndex];
    if (item && !state.busy) renderPhoto(item);
  });
  document.getElementById('review-back').addEventListener('click', function () { workspace.classList.remove('review-show-detail'); focusSelectedItem(); });

  /** @param {Suggestion} candidate @param {ComplementField[]} targets @returns {number} */
  function applyEmptySuggestion(candidate, targets) {
    const form = document.getElementById('review-edit-form');
    let applied = 0;
    targets.forEach(function (name) {
      if (!form.elements[name].value.trim() && !form.elements[name].readOnly && candidate[name]) {
        form.elements[name].value = candidate[name]; applied += 1;
      }
    });
    const item = state.items[state.selectedIndex];
    if (applied && item) {
      selectedSuggestions.set(state.scope + ':' + item.id, {evidence_urls: candidate.evidence_urls.slice(0, 5), reason: candidate.reason || null});
      rememberEditDraft(); renderQueue();
    }
    return applied;
  }

  /** @param {Suggestion[]} candidates @param {ComplementField[]} targets */
  function renderSuggestionChoices(candidates, targets = emptyComplementFields()) {
    const container = document.getElementById('review-ai-results');
    container.replaceChildren();
    const item = state.items[state.selectedIndex];
    if (!item) return;
    if (candidates.length === 1) {
      const applied = applyEmptySuggestion(candidates[0], targets);
      if (!applied) showNotice('空欄に入れられる情報を特定できませんでした。手入力で補ってください。');
      const sources = document.createElement('details');
      const summary = document.createElement('summary'); summary.textContent = 'AIの出典'; sources.appendChild(summary);
      candidates[0].evidence_urls.forEach(function (url) {
        const link = document.createElement('a'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        link.className = 'link-secondary small d-block text-break'; link.textContent = url; sources.appendChild(link);
      });
      document.getElementById('review-candidates').appendChild(sources);
      return;
    }
    const hint = document.createElement('p'); hint.textContent = '店舗を1つに特定できませんでした。候補と出典を確認してください。'; container.appendChild(hint);
    candidates.forEach(function (candidate) {
      const row = document.createElement('div'); row.className = 'mb-2';
      const choose = document.createElement('button'); choose.type = 'button'; choose.className = 'btn btn-outline-secondary btn-sm';
      choose.textContent = [candidate.shop_name, candidate.area].filter(Boolean).join(' / ');
      choose.addEventListener('click', function () {
        if (state.busy || state.items[state.selectedIndex]?.id !== item.id) return;
        applyEmptySuggestion(candidate, targets); container.replaceChildren(); updateApprovalState();
      });
      row.appendChild(choose);
      candidate.evidence_urls.forEach(function (url) { const link = document.createElement('a'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.className = 'link-secondary small d-block text-break'; link.textContent = url; row.appendChild(link); });
      container.appendChild(row);
    });
  }

  function runComplement() {
    const item = state.items[state.selectedIndex];
    const targets = emptyComplementFields();
    if (!item || state.busy || !targets.length || workspace.dataset.readOnly === 'true' || item.review_status === 'rejected') return;
    rememberEditDraft(); rememberSupplement();
    const requestScope = state.scope;
    const draft = editDrafts.get(requestScope + ':' + item.id);
    const values = draft ? draft.values : savedEditValues(item);
    const body = {expected_version: draft ? draft.expectedVersion : item.version,
      shop_version: draft ? draft.shopVersion : item.shop ? item.shop.version : null,
      draft: values, supplement: supplementInput.value, reference_url: referenceInput.value || null};
    const operation = setBusy(true, aiButton, 'AIで読み込み中…'); clearError(); clearNotice();
    document.getElementById('review-ai-results').replaceChildren();
    apiJson('/api/admin/reviews/' + item.id + '/suggestions', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
      .then(function (result) {
        if (state.scope !== requestScope || state.items[state.selectedIndex]?.id !== item.id) return;
        if (result.candidates.length) renderSuggestionChoices(result.candidates, targets);
        else showNotice(result.unresolved_reason || '空欄を補完できませんでした。補足を追加するか、手入力してください。');
      }).catch(function (error) {
        console.error('Review suggestion failed', {mentionId: item.id, error: error});
        if (error instanceof ReviewConflictError) setError('AIで再読み込みできませんでした。入力を保持しています：' + error.message, function () { refreshConflict(item, requestScope); }, '最新情報を読み込む');
        else setError('AIで再読み込みできませんでした。入力を保持しています：' + error.message);
      }).finally(function () { setBusy(false, null, '', operation); });
  }
  aiButton.addEventListener('click', runComplement);

  loadQueue(true, null, readFilterInputs());
})();
