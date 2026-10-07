// Do not make core actions depend on JavaScript; this file only enhances server-rendered flows.

(function () {
  'use strict';

  /** @type {HTMLDetailsElement | null} */
  const accountMenu = document.querySelector('details.site-account-menu');
  if (accountMenu) {
    document.addEventListener('click', function (event) {
      if (event.target instanceof Node && !accountMenu.contains(event.target)) {
        accountMenu.open = false;
      }
    });
    accountMenu.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && accountMenu.open) {
        event.preventDefault();
        accountMenu.open = false;
        accountMenu.querySelector('summary').focus();
      }
    });
    accountMenu.addEventListener('focusout', function (event) {
      if (event.relatedTarget instanceof Node && !accountMenu.contains(event.relatedTarget)) {
        accountMenu.open = false;
      }
    });
  }

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

  function csrfHeaders(extra) {
    var headers = extra || {};
    if (csrfToken) {
      headers['X-CSRF-Token'] = csrfToken;
    }
    return headers;
  }

  class ShopConflictError extends Error {}

  function readJson(response) {
    return response.json().catch(function (error) {
      throw new Error(
        'Invalid JSON response: status=' + response.status + ', error=' + error.message
      );
    }).then(function (payload) {
      if (!response.ok) {
        if (response.status === 409) throw new ShopConflictError(payload.detail || '店舗情報が更新されています。');
        throw new Error(payload.detail || payload.error || ('HTTP ' + response.status));
      }
      return payload;
    });
  }

  function showActionError(message, shopId) {
    var box = document.getElementById('global-action-error');
    if (!box) {
      box = document.createElement('div');
      box.id = 'global-action-error';
      box.className = 'alert alert--error alert-danger';
      box.setAttribute('role', 'alert');
      var mainContainer = document.querySelector('.main-content .container, .main-content .container-fluid');
      if (mainContainer) mainContainer.prepend(box);
    }
    box.textContent = message;
    if (shopId) {
      const link = document.createElement('a');
      link.href = '/shop/' + encodeURIComponent(shopId) + '?edit=true';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = ' 最新の店舗情報を別のタブで確認';
      box.appendChild(link);
    }
    box.tabIndex = -1;
    box.focus();
  }

  function clearActionError() {
    var box = document.getElementById('global-action-error');
    if (box) box.remove();
  }

  function syncActionVersions(shopId, expectedVersion, version) {
    if (!Number.isSafeInteger(version) || version <= expectedVersion) {
      throw new Error('Invalid shop version response payload');
    }
    document.querySelectorAll('[data-shop-version]').forEach(function (control) {
      if (control.dataset.shopId === shopId && Number(control.dataset.shopVersion) === expectedVersion) {
        control.dataset.shopVersion = String(version);
      }
    });
  }

  var resultHeading = document.getElementById('result-heading');
  var resultStatus = document.getElementById('result-status');
  var searchResults = document.getElementById('search-results');
  var resultFocusStorageKey = 'meshi-archive:focus-search-results';

  /** @param {'filter'|'results'} target @returns {void} */
  function saveResultFocusRequest(target = 'results') {
    try {
      window.sessionStorage.setItem(resultFocusStorageKey, target);
    } catch (error) {
      console.error('Result focus request could not be saved', { error: error.message });
    }
  }

  /** @returns {string|null} */
  function takeResultFocusRequest() {
    try {
      var requested = window.sessionStorage.getItem(resultFocusStorageKey);
      if (requested) window.sessionStorage.removeItem(resultFocusStorageKey);
      return requested;
    } catch (error) {
      console.error('Result focus request could not be restored', { error: error.message });
      return null;
    }
  }

  function focusResultHeading(message) {
    if (message && resultStatus) resultStatus.textContent = message;
    if (!resultHeading) return;
    resultHeading.tabIndex = -1;
    window.requestAnimationFrame(function () {
      resultHeading.focus({ preventScroll: false });
    });
  }

  function setResultsLoading() {
    if (searchResults) searchResults.setAttribute('aria-busy', 'true');
    if (resultStatus) resultStatus.textContent = '検索しています。';
  }

  const requestedFocus = takeResultFocusRequest();
  var focusFilterFromHash = window.location.hash === '#q-input';
  if (requestedFocus && requestedFocus !== 'filter' && !focusFilterFromHash) {
    focusResultHeading('検索結果を更新しました。');
  }
  var filterForm = document.getElementById('filter-form');
  if (filterForm) {
    if (focusFilterFromHash || requestedFocus === 'filter') {
      if (requestedFocus && resultStatus) resultStatus.textContent = '検索結果を更新しました。';
      var filterFocusTarget = document.getElementById('q-input');
      if (filterFocusTarget) {
        window.requestAnimationFrame(function () {
          filterFocusTarget.focus();
        });
      }
    }
    filterForm.addEventListener('submit', function () {
      saveResultFocusRequest('filter');
      setResultsLoading();
    });
    window.addEventListener('pageshow', function (event) {
      filterForm.reset();
      if (event.persisted) {
        if (searchResults) searchResults.removeAttribute('aria-busy');
        if (resultStatus) resultStatus.textContent = '';
      }
    });
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest(
      '[data-result-navigation], .pagination__control[href], .pagination__page[href]'
    );
    if (!link) return;
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) return;
    saveResultFocusRequest();
    setResultsLoading();
  });

  document.querySelectorAll('form[data-confirm-message]').forEach(function (form) {
    let submitting = false;
    window.addEventListener('pageshow', function () { submitting = false; });
    form.addEventListener('submit', function (event) {
      if (submitting) {
        event.preventDefault();
        return;
      }
      var message = form.getAttribute('data-confirm-message');
      if (message && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      submitting = true;
    });
  });

  document.addEventListener('click', function (event) {
    var button = event.target.closest('.btn-visit-toggle');
    if (!button || button.disabled) return;

    var shopId = button.dataset.shopId;
    const expectedVersion = Number(button.dataset.shopVersion);
    var restoreButtonFocus = document.activeElement === button;
    clearActionError();
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    fetch('/shop/' + shopId + '/visited', {
      method: 'POST',
      headers: csrfHeaders({ 'X-Shop-Version': String(expectedVersion) }),
    })
      .then(readJson)
      .then(function (data) {
        if (
          typeof data.is_visited !== 'boolean'
          || !(
            data.visited_at === null
            || typeof data.visited_at === 'string'
          )
          || (data.is_visited && typeof data.visited_at !== 'string')
        ) {
          throw new Error('Invalid visit status response payload');
        }
        syncActionVersions(shopId, expectedVersion, data.version);
        var wasVisited = button.dataset.visited === 'true';
        button.dataset.visited = data.is_visited ? 'true' : 'false';
        button.textContent = data.is_visited ? '訪問済み' : '未訪問';
        button.classList.toggle('badge--visited', data.is_visited);
        button.classList.toggle('badge--unvisited', !data.is_visited);
        button.title = data.is_visited
          ? (data.visited_at
              ? '訪問済み（訪問日: ' + data.visited_at + '）'
              : '訪問済み（訪問日未設定）')
          : '未訪問';
        if (new URLSearchParams(window.location.search).has('status')) {
          saveResultFocusRequest();
          setResultsLoading();
          window.location.reload();
        }
      })
      .catch(function (error) {
        console.error('Visit status request failed', {
          endpoint: '/shop/' + shopId + '/visited',
          error: error.message,
        });
        showActionError(error instanceof ShopConflictError
          ? '店舗情報が更新されています。最新の内容を確認して一覧を読み直してから操作してください。'
          : '訪問状況を更新できませんでした。時間をおいて、もう一度お試しください。',
        error instanceof ShopConflictError ? shopId : null);
      })
      .finally(function () {
        button.disabled = false;
        button.removeAttribute('aria-busy');
        var buttonFocusWasLost = (
          document.activeElement === document.body
          || document.activeElement === document.documentElement
        );
        if (restoreButtonFocus && buttonFocusWasLost && document.contains(button)) {
          button.focus({ preventScroll: true });
        }
      });
  });

  // Do not use mouseenter or mouseleave because delegated listeners need bubbling events.
  document.addEventListener('mouseover', function (event) {
    var star = event.target.closest('.star-rating:not(.star-rating--readonly) .star');
    if (!star) return;
    var container = star.closest('.star-rating');
    var value = parseInt(star.dataset.value, 10);
    container.querySelectorAll('.star').forEach(function (item, index) {
      item.classList.toggle('star--hover', (index + 1) <= value);
    });
  });

  document.addEventListener('mouseout', function (event) {
    var star = event.target.closest('.star-rating:not(.star-rating--readonly) .star');
    if (!star) return;
    var container = star.closest('.star-rating');
    container.querySelectorAll('.star').forEach(function (item) {
      item.classList.remove('star--hover');
    });
  });

  document.addEventListener('click', function (event) {
    var star = event.target.closest('.star-rating:not(.star-rating--readonly) .star');
    if (!star) return;
    var container = star.closest('.star-rating');
    if (container.dataset.busy === 'true') return;

    var shopId = container.dataset.shopId;
    const expectedVersion = Number(container.dataset.shopVersion);
    var clicked = parseInt(star.dataset.value, 10);
    var current = parseInt(container.dataset.rating, 10) || 0;
    var newRating = (clicked === current) ? 0 : clicked;
    var restoreStarFocus = document.activeElement === star;
    clearActionError();

    container.dataset.busy = 'true';
    container.setAttribute('aria-busy', 'true');
    container.querySelectorAll('.star').forEach(function (item) {
      item.disabled = true;
    });

    fetch('/shop/' + shopId + '/rating', {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json', 'X-Shop-Version': String(expectedVersion) }),
      body: JSON.stringify({ rating: newRating }),
    })
      .then(readJson)
      .then(function (data) {
        if (
          data.rating !== null
          && (!Number.isInteger(data.rating) || data.rating < 0 || data.rating > 5)
        ) {
          throw new Error('Invalid rating response payload');
        }
        syncActionVersions(shopId, expectedVersion, data.version);
        var rating = data.rating === null ? 0 : data.rating;
        container.dataset.rating = rating;
        container.querySelectorAll('.star').forEach(function (item, index) {
          item.classList.toggle('star--active', rating > 0 && (index + 1) <= rating);
        });
        container.setAttribute('aria-label', '評価 ' + rating + '点');
        var activeSort = new URLSearchParams(window.location.search).get('sort');
        if (activeSort === 'rating_asc' || activeSort === 'rating_desc') {
          saveResultFocusRequest();
          setResultsLoading();
          window.location.reload();
        }
      })
      .catch(function (error) {
        console.error('Rating request failed', {
          endpoint: '/shop/' + shopId + '/rating',
          error: error.message,
        });
        showActionError(error instanceof ShopConflictError
          ? '店舗情報が更新されています。最新の内容を確認して一覧を読み直してから操作してください。'
          : '評価を更新できませんでした。時間をおいて、もう一度お試しください。',
        error instanceof ShopConflictError ? shopId : null);
      })
      .finally(function () {
        container.dataset.busy = 'false';
        container.removeAttribute('aria-busy');
        container.querySelectorAll('.star').forEach(function (item) {
          item.disabled = false;
        });
        var starFocusWasLost = (
          document.activeElement === document.body
          || document.activeElement === document.documentElement
        );
        if (restoreStarFocus && starFocusWasLost && document.contains(star)) {
          star.focus({ preventScroll: true });
        }
      });
  });

  var importForm = document.getElementById('import-form');
  if (importForm) {
    const fileInput = importForm.querySelector('input[type="file"]');
    const fileError = document.getElementById('import-file-error');
    fileInput.addEventListener('change', function () {
      fileError.hidden = true;
      fileInput.removeAttribute('aria-invalid');
    });
    importForm.addEventListener('submit', function (event) {
      if (!fileInput || !fileInput.files.length) {
        event.preventDefault();
        fileError.hidden = false;
        fileInput.setAttribute('aria-invalid', 'true');
        fileInput.focus();
      }
    });
  }

  var importApplyForm = document.getElementById('import-apply-form');
  if (importApplyForm) {
    let submitting = false;
    window.addEventListener('pageshow', function () { submitting = false; });
    const applyFileInput = importApplyForm.querySelector('input[type="file"]');
    const applyFileError = document.getElementById('apply-file-error');
    applyFileInput.addEventListener('change', function () {
      applyFileError.hidden = true;
      applyFileInput.removeAttribute('aria-invalid');
    });
    importApplyForm.addEventListener('submit', function (event) {
      if (submitting) {
        event.preventDefault();
        return;
      }
      if (!applyFileInput || !applyFileInput.files.length) {
        event.preventDefault();
        applyFileError.hidden = false;
        applyFileInput.setAttribute('aria-invalid', 'true');
        applyFileInput.focus();
        return;
      }
      if (!window.confirm('表示した店舗・項目だけを更新します。空欄の項目は消去されます。適用しますか？')) {
        event.preventDefault();
        return;
      }
      submitting = true;
    });
  }

  /** @param {HTMLImageElement} image @returns {void} */
  function removeBrokenShopImage(image) {
    var media = image.closest('[data-shop-media]');
    var imageLink = image.closest('[data-shop-image-link]');
    var card = image.closest('.place-card');
    var row = image.closest('[data-shop-row]');
    var host = 'invalid';
    try {
      host = new URL(image.currentSrc || image.src).hostname;
    } catch (error) {
      console.error('Shop image URL could not be parsed', {
        shopId: row ? row.dataset.shopId : null,
        error: error.message,
      });
    }
    console.warn('Shop image could not be loaded', {
      shopId: row ? row.dataset.shopId : null,
      host: host,
    });
    if (imageLink) {
      if (imageLink.contains(document.activeElement)) {
        var titleLink = card ? card.querySelector('.place-card__title a') : null;
        if (titleLink) titleLink.focus({ preventScroll: true });
      }
      imageLink.remove();
    } else {
      image.remove();
    }
    if (media) {
      media.classList.add('place-card__media--empty');
      media.setAttribute('aria-hidden', 'true');
    }
    if (card) card.classList.remove('place-card--with-image');
  }

  document.addEventListener('error', function (event) {
    var image = event.target;
    if (!image || !image.matches || !image.matches('[data-shop-image]')) return;
    removeBrokenShopImage(image);
  }, true);

  document.querySelectorAll('[data-shop-image]').forEach(function (image) {
    if (image.complete && image.naturalWidth === 0) removeBrokenShopImage(image);
  });

  var detailDialog = document.getElementById('shop-detail-dialog');
  if (!detailDialog || typeof detailDialog.showModal !== 'function') return;

  var detailBody = document.getElementById('detail-dialog-body');
  var detailTitle = document.getElementById('detail-dialog-title');
  var detailStatus = document.getElementById('detail-dialog-status');
  var tableWrapper = document.querySelector('.place-results');
  if (!detailBody || !detailTitle) {
    console.error('Shop detail dialog is missing required content elements');
    return;
  }
  if (!detailStatus) {
    console.error('Shop detail dialog is missing #detail-dialog-status');
  }
  detailBody.removeAttribute('aria-live');
  var activeTrigger = null;
  var activeShopId = null;
  var activeRowId = null;
  var activeRequest = null;
  var requestSequence = 0;
  var lastRequestUrl = '';
  var listScrollY = window.scrollY;
  var listTableScrollTop = tableWrapper ? tableWrapper.scrollTop : 0;

  function setDetailStatus(message) {
    if (detailStatus) detailStatus.textContent = message;
  }

  function detailStateUrl(shopId, rowId) {
    var url = new URL(window.location.href);
    url.searchParams.set('detail', shopId);
    url.searchParams.set('row', rowId);
    return url.pathname + '?' + url.searchParams.toString();
  }

  function listUrlWithoutDetail() {
    var url = new URL(window.location.href);
    url.searchParams.delete('detail');
    url.searchParams.delete('row');
    var query = url.searchParams.toString();
    return url.pathname + (query ? '?' + query : '');
  }

  function detailRequestUrl(shopId) {
    var returnTo = listUrlWithoutDetail();
    return '/shop/' + encodeURIComponent(shopId) + '?return_to=' + encodeURIComponent(returnTo);
  }

  function detailTrigger(shopId) {
    if (!/^\d+$/.test(String(shopId))) return null;
    return document.querySelector('[data-shop-detail][data-shop-id="' + String(shopId) + '"]');
  }

  function setCurrentRow(shopId) {
    var normalizedShopId = shopId === null ? null : String(shopId);
    document.querySelectorAll('[data-shop-row]').forEach(function (row) {
      var isCurrent = normalizedShopId !== null && row.dataset.shopId === normalizedShopId;
      row.classList.toggle('is-current', isCurrent);
      if (isCurrent) {
        row.setAttribute('aria-current', 'true');
      } else {
        row.removeAttribute('aria-current');
      }
    });
  }

  function setDialogLoading() {
    detailTitle.textContent = '店舗詳細';
    setDetailStatus('店舗情報を読み込んでいます。');
    detailBody.setAttribute('aria-busy', 'true');
    detailBody.innerHTML = '<div class="detail-loading"><span class="loading-indicator" aria-hidden="true"></span><p>店舗情報を読み込んでいます。</p></div>';
  }

  function renderDialogError(requestUrl, error) {
    console.error('Shop detail request failed', {
      endpoint: requestUrl,
      error: error.message,
    });
    detailTitle.textContent = '店舗詳細';
    setDetailStatus('');
    detailBody.innerHTML = '<div class="detail-error alert alert-danger" role="alert"><p class="detail-error__title fw-bold">店舗情報を読み込めませんでした</p><p>通信状態を確認して、もう一度お試しください。</p><button type="button" class="btn btn--primary btn-primary" data-dialog-retry>再試行</button></div>';
    var retryButton = detailBody.querySelector('[data-dialog-retry]');
    if (retryButton) {
      window.requestAnimationFrame(function () {
        if (detailDialog.open && document.contains(retryButton)) retryButton.focus();
      });
    }
  }

  function ensureRowVisible(trigger) {
    var row = trigger.closest('[data-shop-row]') || trigger;
    if (!tableWrapper || !tableWrapper.contains(row)) {
      row.scrollIntoView({ behavior: 'auto', block: 'nearest', inline: 'nearest' });
      return;
    }
    var wrapperStyle = window.getComputedStyle(tableWrapper);
    var wrapperCanScroll = (
      /^(auto|scroll|overlay)$/.test(wrapperStyle.overflowY)
      && tableWrapper.scrollHeight > tableWrapper.clientHeight + 1
    );
    if (!wrapperCanScroll) {
      row.scrollIntoView({ behavior: 'auto', block: 'nearest', inline: 'nearest' });
      return;
    }
    var wrapperRect = tableWrapper.getBoundingClientRect();
    var rowRect = row.getBoundingClientRect();
    var tableHeader = tableWrapper.querySelector('thead');
    var headerHeight = tableHeader ? tableHeader.getBoundingClientRect().height : 0;
    var visibleTop = wrapperRect.top + headerHeight;
    if (rowRect.top < visibleTop) {
      tableWrapper.scrollTop -= visibleTop - rowRect.top;
    } else if (rowRect.bottom > wrapperRect.bottom) {
      tableWrapper.scrollTop += rowRect.bottom - wrapperRect.bottom;
    }
  }

  function restoreListPosition() {
    var historyState = window.history.state || {};
    var stateShopId = historyState.rowShopId || historyState.focusedShopId;
    var savedScrollY = historyState.listScrollY;
    var savedTableScrollTop = historyState.listTableScrollTop;
    var restoreScrollY = (
      typeof savedScrollY === 'number'
      && Number.isFinite(savedScrollY)
      && savedScrollY >= 0
    ) ? savedScrollY : listScrollY;
    var restoreTableScrollTop = (
      typeof savedTableScrollTop === 'number'
      && Number.isFinite(savedTableScrollTop)
      && savedTableScrollTop >= 0
    ) ? savedTableScrollTop : listTableScrollTop;
    var rowId = activeRowId || stateShopId || activeShopId;
    var trigger = activeTrigger;
    if (!trigger || !document.contains(trigger)) trigger = detailTrigger(rowId);
    window.scrollTo({ top: restoreScrollY, behavior: 'auto' });
    if (tableWrapper) tableWrapper.scrollTop = restoreTableScrollTop;
    if (trigger && document.contains(trigger)) {
      window.requestAnimationFrame(function () {
        ensureRowVisible(trigger);
        trigger.focus({ preventScroll: true });
      });
      return;
    }
    focusResultHeading('元の店舗は現在の検索結果にありません。');
  }

  function closeDialog(options) {
    var settings = options || {};
    requestSequence += 1;
    if (activeRequest) {
      activeRequest.abort();
      activeRequest = null;
    }
    if (detailDialog.open) detailDialog.close();
    document.documentElement.classList.remove('is-dialog-open');
    setCurrentRow(null);
    setDetailStatus('');
    detailBody.removeAttribute('aria-busy');
    detailBody.replaceChildren();
    if (settings.restoreFocus !== false) restoreListPosition();
  }

  function loadDetail(requestUrl) {
    requestSequence += 1;
    var sequence = requestSequence;
    if (activeRequest) activeRequest.abort();
    activeRequest = new AbortController();
    lastRequestUrl = requestUrl;
    setDialogLoading();

    fetch(requestUrl, {
      cache: 'no-store',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
      signal: activeRequest.signal,
    })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.text();
      })
      .then(function (html) {
        if (sequence !== requestSequence || !detailDialog.open) return;
        var documentFragment = new DOMParser().parseFromString(html, 'text/html');
        var content = documentFragment.querySelector('[data-shop-detail-content]');
        if (!content) throw new Error('Detail content was missing');
        var title = content.querySelector('[data-detail-title]');
        detailTitle.textContent = title ? title.textContent.trim() : '店舗詳細';
        var embeddedTitleGroup = title ? title.closest('[data-detail-title-group]') : null;
        if (title) title.remove();
        if (embeddedTitleGroup && embeddedTitleGroup.childElementCount === 0) embeddedTitleGroup.remove();
        detailBody.replaceChildren(content);
        setDetailStatus(detailTitle.textContent + 'の店舗情報を読み込みました。');
      })
      .catch(function (error) {
        if (error.name === 'AbortError' || sequence !== requestSequence || !detailDialog.open) return;
        renderDialogError(requestUrl, error);
      })
      .finally(function () {
        if (sequence === requestSequence) {
          activeRequest = null;
          detailBody.removeAttribute('aria-busy');
        }
      });
  }

  function openDetail(shopId, trigger, addHistory, rowId) {
    activeShopId = String(shopId);
    activeRowId = /^\d+$/.test(String(rowId)) ? String(rowId) : activeShopId;
    activeTrigger = trigger || detailTrigger(activeRowId);
    listScrollY = window.scrollY;
    listTableScrollTop = tableWrapper ? tableWrapper.scrollTop : 0;
    setCurrentRow(activeRowId);

    if (addHistory) {
      var listState = Object.assign({}, window.history.state || {}, {
        listScrollY: listScrollY,
        listTableScrollTop: listTableScrollTop,
        focusedShopId: activeRowId,
        rowShopId: activeRowId,
      });
      window.history.replaceState(listState, '', window.location.href);
      window.history.pushState({
        detailShopId: activeShopId,
        rowShopId: activeRowId,
        listScrollY: listScrollY,
        listTableScrollTop: listTableScrollTop,
      }, '', detailStateUrl(activeShopId, activeRowId));
    }

    if (!detailDialog.open) detailDialog.showModal();
    document.documentElement.classList.add('is-dialog-open');
    var detailLink = trigger && trigger.dataset.shopId === activeShopId
      ? trigger
      : detailTrigger(activeShopId);
    loadDetail(detailLink ? detailLink.href : detailRequestUrl(activeShopId));
  }

  function requestDialogClose() {
    if (window.history.state && window.history.state.detailShopId) {
      window.history.back();
      return;
    }
    window.history.replaceState(window.history.state, '', listUrlWithoutDetail());
    closeDialog();
  }

  function syncDialogWithUrl() {
    var params = new URLSearchParams(window.location.search);
    var shopId = params.get('detail');
    if (!shopId || !/^\d+$/.test(shopId)) {
      if (detailDialog.open) {
        closeDialog();
      } else {
        document.documentElement.classList.remove('is-dialog-open');
        setCurrentRow(null);
      }
      return;
    }
    var rowId = params.get('row');
    if (!rowId || !/^\d+$/.test(rowId)) rowId = shopId;
    var trigger = detailTrigger(rowId);
    openDetail(shopId, trigger, false, rowId);
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a[data-shop-detail]');
    if (!link) return;
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    openDetail(link.dataset.shopId, link, true, link.dataset.shopId);
  });

  detailDialog.addEventListener('click', function (event) {
    if (event.target === detailDialog || event.target.closest('[data-dialog-close]')) {
      requestDialogClose();
      return;
    }
    if (event.target.closest('[data-dialog-retry]')) {
      var closeButton = detailDialog.querySelector('[data-dialog-close]');
      if (closeButton) closeButton.focus();
      loadDetail(lastRequestUrl);
    }
  });

  detailDialog.addEventListener('cancel', function (event) {
    event.preventDefault();
    requestDialogClose();
  });

  window.addEventListener('popstate', function () {
    syncDialogWithUrl();
  });

  syncDialogWithUrl();
})();
