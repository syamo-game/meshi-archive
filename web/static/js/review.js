(function () {
  'use strict';

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';
  var state = { items: [], selectedIndex: -1, nextCursor: null, busy: false, scope: 'identity', filterQuery: '' };
  /** @typedef {{shop_name: string, branch_name: string, area: string, category: string, address: string, phone: string, canonical_url: string}} EditValues */
  /** @typedef {{values: EditValues, expectedVersion: number, shopVersion: number|null}} EditDraft */
  /** @type {Map<string, EditDraft>} */
  const editDrafts = new Map();
  const disabledBeforeBusy = new Map();
  const labelsBeforeBusy = new Map();
  const candidateButtons = [];
  const scopeLabels = { identity: '店・支店を確認', metadata: 'エリア・カテゴリを確認' };
  const statusLabels = {
    pending: '未確認', deferred: 'あとで確認', approved: '確認済み', rejected: 'お店ではないと判断',
  };
  const reasonLabels = new Map([
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
  var approveButton = document.getElementById('review-approve');
  const editSubmit = document.getElementById('review-edit-submit');
  const filterSubmit = document.getElementById('review-filter-submit');
  const mergeSubmit = document.getElementById('review-merge-submit');

  if (!workspace || !queue || !filter || !errorBox || !errorText || !retryButton || !noticeBox || !noticeText || !loadMore || !evidence || !decisionPanel || !empty || !approveButton) return;

  workspace.tabIndex = 0;

  function setError(message, retry, retryLabel = '再試行') {
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

  function canApproveCurrent(item) {
    if (!item || hasUnsavedEdits(item)) return false;
    if (state.scope === 'metadata') return Boolean(item.shop);
    return Boolean(item.shop)
      || (item.difference_type !== 'extraction_not_found' && item.extracted_name !== '（店舗名未特定）');
  }

  function updateApprovalState() {
    const item = state.items[state.selectedIndex];
    approveButton.disabled = state.busy || !canApproveCurrent(item);
    approveButton.title = item && hasUnsavedEdits(item)
      ? '入力を変更しています。「修正して確認を終える」で保存してください。'
      : (!canApproveCurrent(item) ? '候補の選択または編集が必要です。' : '');
    candidateButtons.forEach(function (button) {
      button.disabled = state.busy || Boolean(item && hasUnsavedEdits(item));
      button.title = item && hasUnsavedEdits(item)
        ? '入力を変更しています。「修正して確認を終える」で保存してください。' : '';
    });
  }

  function lockBusyControls() {
    [filter, workspace, errorBox].forEach(function (container) {
      container.querySelectorAll('button, input, select, textarea').forEach(function (control) {
        if (!disabledBeforeBusy.has(control)) disabledBeforeBusy.set(control, control.disabled);
        control.disabled = true;
      });
    });
  }

  function setBusy(busy, button, label) {
    state.busy = busy;
    workspace.setAttribute('aria-busy', String(busy));
    filter.setAttribute('aria-busy', String(busy));
    if (busy) {
      lockBusyControls();
      if (button) {
        if (!labelsBeforeBusy.has(button)) labelsBeforeBusy.set(button, button.textContent);
        button.textContent = label;
        button.setAttribute('aria-busy', 'true');
      }
    } else {
      disabledBeforeBusy.forEach(function (disabled, control) { control.disabled = disabled; });
      disabledBeforeBusy.clear();
      labelsBeforeBusy.forEach(function (text, control) {
        control.textContent = text;
        control.removeAttribute('aria-busy');
      });
      labelsBeforeBusy.clear();
    }
    updateApprovalState();
  }

  class ReviewConflictError extends Error {}

  function apiJson(url, options) {
    var requestOptions = options || {};
    requestOptions.headers = requestOptions.headers || {};
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
            throw new ReviewConflictError('別の更新がありました。入力内容は保持されています。');
          }
          throw new Error(typeof payload.detail === 'string' ? payload.detail : ('入力内容と接続状況を確認してください（HTTP ' + response.status + '）。'));
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
    document.getElementById('review-result-count').textContent = String(payload.total_count);
    document.getElementById('count-unavailable').textContent = payload.counters.source_unavailable;
    document.getElementById('count-failed').textContent = payload.counters.failed;
    const params = new URLSearchParams(state.filterQuery);
    const context = [scopeLabels[state.scope], statusLabels[params.get('status')]];
    if (params.get('q')) context.push('店名「' + params.get('q') + '」');
    document.getElementById('review-result-context').textContent = context.filter(Boolean).join(' / ');
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
      name.textContent = [item.extracted_name, item.extracted_branch_name].filter(Boolean).join(' / ');
      var meta = document.createElement('span');
      meta.className = 'small text-body-secondary';
      var metaParts = [item.extracted_area || 'エリア不明', reviewReason(item)];
      if (item.review_group_count > 1) {
        metaParts.push('同じ店と思われる項目 ' + item.review_group_count + '件');
      }
      meta.textContent = metaParts.join(' · ');
      button.append(name, meta);
      button.addEventListener('click', function () { selectItem(index); });
      queue.appendChild(button);
    });
    loadMore.hidden = !state.nextCursor;
    if (state.busy) lockBusyControls();
  }

  function reviewReason(item) {
    const code = state.scope === 'metadata' ? item.metadata_difference_type : item.difference_type;
    if (!code) return '確認に回った理由の記録はありません。';
    return reasonLabels.get(code) || '確認に回された項目です。詳細な理由は処理情報で確認できます。';
  }

  function focusSelectedItem() {
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
      if (asset.proxy_url) {
        var image = document.createElement('img');
        image.src = asset.proxy_url;
        image.alt = asset.title || 'Discord投稿の添付画像';
        image.loading = 'lazy';
        image.className = 'img-fluid rounded';
        card.appendChild(image);
      }
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
    if (state.scope === 'metadata') return;
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
      select.textContent = 'この候補で保存して確認を終える';
      select.addEventListener('click', function () {
        submitDecision({ action: 'approve_candidate', candidate_id: candidate.id }, select);
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
      expectedVersion: previous ? previous.expectedVersion : item.version,
      shopVersion: previous ? previous.shopVersion : (item.shop ? item.shop.version : null),
    });
    if (!hasUnsavedEdits(item)) editDrafts.delete(key);
    updateApprovalState();
  }

  function savedEditValues(item) {
    const shop = item.shop || {};
    return {
      shop_name: shop.shop_name || item.extracted_name || '',
      branch_name: state.scope === 'metadata'
        ? (shop.branch_name || '') : (shop.branch_name || item.extracted_branch_name || ''),
      area: shop.area || item.extracted_area || '',
      category: shop.category || item.extracted_category || '',
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

  function fillEditForm(item) {
    var form = document.getElementById('review-edit-form');
    const draft = editDrafts.get(state.scope + ':' + item.id);
    const values = draft ? draft.values : savedEditValues(item);
    Object.keys(values).forEach(function (name) { form.elements[name].value = values[name]; });
    updateApprovalState();
  }

  function renderItem() {
    var item = state.items[state.selectedIndex];
    if (!item) {
      evidence.hidden = true;
      decisionPanel.hidden = true;
      empty.hidden = false;
      updateApprovalState();
      return;
    }
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
      state.scope === 'metadata' ? item.metadata_difference_type : item.difference_type
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
    document.querySelector('.review-merge').hidden = state.scope === 'metadata';
    document.getElementById('review-reject').hidden = state.scope === 'metadata';
    document.getElementById('review-edit-form').elements.shop_name.readOnly = state.scope === 'metadata';
    document.getElementById('review-edit-form').elements.branch_name.readOnly = state.scope === 'metadata';
    document.getElementById('review-edit-form').elements.canonical_url.readOnly = state.scope === 'metadata';
    setText('review-edit-scope-hint', state.scope === 'metadata'
      ? 'エリア・カテゴリを確認します。住所と電話番号も修正できます。店名・支店名・店舗URLを直す場合は「店・支店を確認」に切り替えてください。'
      : '元の投稿と店名・支店名を見比べてください。修正すると、同じ店に登録されている店舗情報も更新されます。');
    renderAssets(item);
    renderCandidates(item);
    fillEditForm(item);
    renderQueue();
  }

  function selectItem(index) {
    if (state.busy || index < 0 || index >= state.items.length) return;
    state.selectedIndex = index;
    renderItem();
    focusSelectedItem();
  }

  /** @param {{scope: string, mentionId: number}|null} [discardDraft] */
  function loadQueue(reset, trigger, query = state.filterQuery, discardDraft = null) {
    if (state.busy) return Promise.resolve();
    setBusy(true, trigger || filterSubmit, '読み込み中…');
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
        if (discardDraft) {
          editDrafts.delete(discardDraft.scope + ':' + discardDraft.mentionId);
          const selectedIndex = state.items.findIndex(function (item) { return item.id === discardDraft.mentionId; });
          if (selectedIndex >= 0) state.selectedIndex = selectedIndex;
          showNotice(selectedIndex >= 0
            ? '最新の情報を読み込みました。必要な変更を入力し直してください。'
            : '最新の一覧を読み込みました。対象の項目が見つからない場合は、絞り込み条件や追加読み込みで確認してください。');
        }
        renderQueue();
        renderItem();
      })
      .catch(function (error) {
        console.error('Review queue load failed', { reset: reset, error: error });
        setError('確認する項目を読み込めませんでした。入力内容は保持されています: ' + error.message, function () {
          loadQueue(reset, retryButton, query, discardDraft);
        });
      })
      .finally(function () { setBusy(false); })
      .then(function () {
        if (!trigger || !errorBox.hidden) return;
        if (state.selectedIndex >= 0) focusSelectedItem();
        else filterSubmit.focus({ preventScroll: true });
      });
  }

  function submitDecision(payload, trigger) {
    var item = state.items[state.selectedIndex];
    if (!item || state.busy) return;
    rememberEditDraft();
    if (payload.action === 'approve_current' && !canApproveCurrent(item)) return;
    if (payload.action === 'approve_candidate' && hasUnsavedEdits(item)) return;
    const draft = editDrafts.get(state.scope + ':' + item.id);
    payload.expected_version = draft ? draft.expectedVersion : item.version;
    payload.scope = state.scope;
    if (draft) {
      payload.shop_version = draft.shopVersion;
    } else if (item.shop) {
      payload.shop_version = item.shop.version;
    }
    sendDecision(item, JSON.stringify(payload), state.scope, trigger);
  }

  function refreshCounters() {
    setBusy(true, retryButton.hidden ? null : retryButton, '更新中…');
    clearError();
    return apiJson('/api/admin/reviews?' + filterParams(null), { method: 'GET' })
      .then(updateCounters)
      .catch(function (error) {
        console.error('Review counters refresh failed after saving', error);
        setError('保存は完了しましたが、件数を更新できませんでした: ' + error.message, refreshCounters);
      })
      .finally(function () { setBusy(false); })
      .then(function () { if (errorBox.hidden) focusSelectedItem(); });
  }

  function sendDecision(item, requestBody, requestScope, trigger) {
    if (state.busy) return;
    setBusy(true, trigger, '保存中…');
    clearError();
    clearNotice();
    apiJson('/api/admin/reviews/' + item.id + '/decision', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: requestBody,
    })
      .then(function (result) {
        var automaticallyResolvedIds = result.automatically_resolved_mention_ids || [];
        var removedIds = new Set([item.id].concat(automaticallyResolvedIds));
        removedIds.forEach(function (id) { editDrafts.delete(requestScope + ':' + id); });
        var nextItemId = nextRemainingItemId(
          state.items,
          state.selectedIndex,
          removedIds
        );
        state.items = state.items.filter(function (queuedItem) {
          return !removedIds.has(queuedItem.id);
        });
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
        if (!state.items.length) {
          setBusy(false);
          return loadQueue(true).then(function () {
            if (!errorBox.hidden) return;
            if (state.selectedIndex >= 0) focusSelectedItem();
            else filterSubmit.focus({ preventScroll: true });
          });
        }
        renderQueue();
        renderItem();
        return refreshCounters();
      })
      .catch(function (error) {
        console.error('Review decision save failed', { mentionId: item.id, error: error });
        if (error instanceof ReviewConflictError) {
          const query = state.filterQuery;
          setError('別の更新があり、保存できませんでした。入力内容は保持されています。最新の情報で編集し直す場合は、入力内容を控えてから「最新情報を読み込む」を押してください。', function () {
            if (!window.confirm('この項目の未保存の入力を破棄し、最新の情報を読み込みます。よろしいですか？')) return;
            loadQueue(true, retryButton, query, { scope: requestScope, mentionId: item.id });
          }, '最新情報を読み込む');
          return;
        }
        setError('データ確認結果を保存できませんでした。入力内容は保持されています: ' + error.message, function () {
          sendDecision(item, requestBody, requestScope, retryButton);
        });
      })
      .finally(function () { setBusy(false); });
  }

  filter.addEventListener('submit', function (event) {
    event.preventDefault();
    if (state.busy) return;
    loadQueue(true, filterSubmit, readFilterInputs());
  });
  retryButton.addEventListener('click', function () {
    if (retryAction && !state.busy) retryAction();
  });
  loadMore.addEventListener('click', function () { loadQueue(false, loadMore); });
  approveButton.addEventListener('click', function () {
    submitDecision({ action: 'approve_current' }, approveButton);
  });
  document.getElementById('review-defer').addEventListener('click', function () {
    if (state.busy) return;
    var note = window.prompt('あとで確認するためのメモ（任意）', '');
    if (note !== null) submitDecision({ action: 'defer', note: note || null }, this);
  });
  document.getElementById('review-reject').addEventListener('click', function () {
    if (state.busy) return;
    submitDecision({ action: 'reject' }, this);
  });

  document.getElementById('review-edit-form').addEventListener('input', rememberEditDraft);
  document.getElementById('review-edit-form').addEventListener('submit', function (event) {
    event.preventDefault();
    var data = new FormData(event.currentTarget);
    submitDecision({
      action: 'edit_and_approve',
      shop: {
        shop_name: String(data.get('shop_name') || ''),
        branch_name: String(data.get('branch_name') || '') || null,
        area: String(data.get('area') || ''),
        category: String(data.get('category') || ''),
        address: String(data.get('address') || '') || null,
        phone: String(data.get('phone') || '') || null,
        canonical_url: String(data.get('canonical_url') || '') || null,
      },
    }, editSubmit);
  });

  var mergeForm = document.getElementById('review-merge-form');
  mergeForm.elements.target_shop_id.addEventListener('change', function () {
    if (state.busy) return;
    var targetId = this.value;
    mergeForm.elements.target_version.value = '';
    if (!targetId) return;
    setBusy(true, mergeSubmit, '確認中…');
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
      .finally(function () { setBusy(false); });
  });
  mergeForm.addEventListener('submit', function (event) {
    event.preventDefault();
    if (state.busy) return;
    var data = new FormData(mergeForm);
    if (!data.get('target_version')) {
      setError('統合先店舗を確認してから実行してください。');
      return;
    }
    if (!window.confirm('投稿との関連を選んだ店舗にまとめ、元の店舗を削除しますか？')) return;
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
    if (key === 'a' && canApproveCurrent(state.items[state.selectedIndex])) {
      event.preventDefault();
      submitDecision({ action: 'approve_current' }, approveButton);
    } else if (key === 'd') {
      event.preventDefault();
      document.getElementById('review-defer').click();
    } else if (key === 'x' && state.scope === 'identity') {
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

  loadQueue(true, null, readFilterInputs());
})();
