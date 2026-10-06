from pathlib import Path


MAIN_SCRIPT = Path("web/static/js/main.js")


def test_list_uses_explicit_server_pagination() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "IntersectionObserver" not in script
    assert "filterForm.submit()" not in script
    assert "filterForm.addEventListener('submit'" in script


def test_detail_drawer_preserves_progressive_navigation_and_history() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "a[data-shop-detail]" in script
    assert "event.button !== 0" in script
    assert "event.metaKey" in script
    assert "event.ctrlKey" in script
    assert "event.shiftKey" in script
    assert "event.altKey" in script
    assert "window.history.pushState" in script
    assert "window.addEventListener('popstate'" in script
    assert "window.history.replaceState" in script
    assert "trigger.focus({ preventScroll: true })" in script


def test_detail_drawer_restores_saved_scroll_and_marks_current_row() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "historyState.listScrollY" in script
    assert "historyState.listTableScrollTop" in script
    assert "top: restoreScrollY" in script
    assert "tableWrapper.scrollTop = restoreTableScrollTop" in script
    assert "ensureRowVisible(trigger)" in script
    assert "setCurrentRow(activeRowId)" in script
    assert "row.setAttribute('aria-current', 'true')" in script
    assert "setCurrentRow(null)" in script


def test_detail_drawer_restores_row_from_url_or_falls_back_to_results() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "var rowId = params.get('row');" in script
    assert "openDetail(shopId, trigger, false, rowId);" in script
    assert "rowShopId: activeRowId" in script
    assert "detailStateUrl(activeShopId, activeRowId)" in script
    assert "trigger = detailTrigger(rowId)" in script
    assert "focusResultHeading('元の店舗は現在の検索結果にありません。')" in script
    assert "if (message && resultStatus) resultStatus.textContent = message;" in script


def test_removed_filter_moves_focus_to_query_input() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "var focusFilterFromHash = window.location.hash === '#q-input'" in script
    assert "filterFocusTarget.focus()" in script


def test_detail_drawer_aborts_stale_requests_and_offers_retry() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "new AbortController()" in script
    assert "activeRequest.abort()" in script
    assert "sequence !== requestSequence" in script
    assert "data-dialog-retry" in script
    assert "loadDetail(lastRequestUrl)" in script
    assert "role=\"alert\"" in script
    assert "detailBody.removeAttribute('aria-busy')" in script


def test_detail_drawer_separates_live_status_and_manages_focus_on_retry() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "document.getElementById('detail-dialog-status')" in script
    assert "detailBody.removeAttribute('aria-live')" in script
    assert "setDetailStatus('店舗情報を読み込んでいます。')" in script
    assert "setDetailStatus(detailTitle.textContent + 'の店舗情報を読み込みました。')" in script
    assert "if (detailDialog.open && document.contains(retryButton)) retryButton.focus();" in script
    assert "var closeButton = detailDialog.querySelector('[data-dialog-close]');" in script
    assert "if (closeButton) closeButton.focus();" in script


def test_detail_drawer_locks_root_while_open() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "document.documentElement.classList.add('is-dialog-open')" in script
    assert "document.documentElement.classList.remove('is-dialog-open')" in script


def test_visible_filters_keep_focus_without_resize_toggles() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")
    template = Path("web/templates/explore.html").read_text(encoding="utf-8")

    assert '<details class="explorer-filters"' not in template
    assert "syncAdvancedFilters" not in script
    assert "advancedFilters.removeAttribute('open')" not in script
    assert "advancedFilters.setAttribute('open', '')" not in script
    assert "window.location.hash === '#q-input'" in script
    assert "filterFocusTarget.focus()" in script
    assert "thead .sort-link" not in script


def test_result_navigation_restores_focus_after_full_page_load() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "window.sessionStorage.setItem(resultFocusStorageKey, 'true')" in script
    assert "window.sessionStorage.getItem(resultFocusStorageKey) === 'true'" in script
    assert "window.sessionStorage.removeItem(resultFocusStorageKey)" in script
    assert "console.error('Result focus request could not be saved'" in script
    assert "console.error('Result focus request could not be restored'" in script
    assert "focusResultHeading('検索結果を更新しました。')" in script
    assert "[data-result-navigation], .pagination__control[href], .pagination__page[href]" in script
    assert "event.defaultPrevented" in script
    assert "event.button !== 0" in script
    assert "event.metaKey" in script
    assert "event.ctrlKey" in script
    assert "event.shiftKey" in script
    assert "event.altKey" in script


def test_filter_form_resets_browser_restored_values_to_server_state() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "window.addEventListener('pageshow'" in script
    assert "filterForm.reset()" in script
    assert "if (event.persisted)" in script
    assert "searchResults.removeAttribute('aria-busy')" in script


def test_inline_admin_actions_send_csrf_and_report_failures() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "headers: csrfHeaders()" in script
    assert "headers: csrfHeaders({ 'Content-Type': 'application/json' })" in script
    assert "Visit status request failed" in script
    assert "Rating request failed" in script
    assert "showActionError" in script
    assert "restoreButtonFocus" in script
    assert "restoreStarFocus" in script
    assert "buttonFocusWasLost" in script
    assert "starFocusWasLost" in script
    assert "box.focus()" in script


def test_inline_admin_actions_keep_facets_and_sorted_results_consistent() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "function updateStatusFacetCounts" not in script
    assert "changeStatusFacetCount" not in script
    assert "new URLSearchParams(window.location.search).has('status')" in script
    assert "activeSort === 'rating_asc' || activeSort === 'rating_desc'" in script
    assert "window.location.reload()" in script
    reload_sequence = (
        "saveResultFocusRequest();\n"
        "          setResultsLoading();\n"
        "          window.location.reload();"
    )
    assert script.count(reload_sequence) == 2


def test_json_parse_errors_fail_loudly() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "Invalid JSON response: status=" in script
    assert "response.json().catch(function (error)" in script
    assert "catch(function () { return {}; })" not in script
    assert "Invalid visit status response payload" in script
    assert "typeof data.is_visited !== 'boolean'" in script
    assert "Invalid rating response payload" in script
    assert "Number.isInteger(data.rating)" in script
    assert "clearActionError()" in script


def test_broken_shop_images_are_logged_and_replaced_with_the_plain_cover() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "function removeBrokenShopImage(image)" in script
    assert "Shop image could not be loaded" in script
    assert "[data-shop-image]" in script
    assert "imageLink.remove()" in script
    assert "if (media) media.remove()" not in script
    assert "media.classList.add('place-card__media--empty')" in script
    assert "media.setAttribute('aria-hidden', 'true')" in script
    assert "imageLink.contains(document.activeElement)" in script
    assert "titleLink.focus({ preventScroll: true })" in script
    assert "card.classList.remove('place-card--with-image')" in script
    assert "image.complete && image.naturalWidth === 0" in script


def test_row_restore_handles_page_scrolling_on_mobile() -> None:
    script = MAIN_SCRIPT.read_text(encoding="utf-8")

    assert "wrapperCanScroll" in script
    assert "window.getComputedStyle(tableWrapper)" in script
    assert "row.scrollIntoView({ behavior: 'auto', block: 'nearest'" in script
