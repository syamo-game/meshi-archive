// Meshi Archive — minimal progressive enhancement
// All features work without JS. JS adds convenience only.

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Auto-submit filter form on select / radio change
  // ---------------------------------------------------------------------------
  var filterForm = document.getElementById('filter-form');
  if (filterForm) {
    filterForm.querySelectorAll('select, input[type="radio"]').forEach(function (el) {
      el.addEventListener('change', function () {
        filterForm.submit();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Visited toggle — delegated so dynamically added rows also respond
  // POST /shop/{id}/visited, update badge in place
  // ---------------------------------------------------------------------------
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.btn-visit-toggle');
    if (!btn) return;

    var shopId = btn.dataset.shopId;
    fetch('/shop/' + shopId + '/visited', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        btn.dataset.visited = data.is_visited ? 'true' : 'false';
        btn.textContent = data.is_visited ? '訪問済み' : '未訪問';
        btn.classList.toggle('badge--visited', data.is_visited);
        btn.classList.toggle('badge--unvisited', !data.is_visited);
        btn.title = data.is_visited
          ? '訪問日: ' + (data.visited_at || '')
          : '未訪問';
      })
      .catch(function (err) { console.error('visited toggle failed', err); });
  });

  // ---------------------------------------------------------------------------
  // Star rating — delegated hover + click
  // Click the active star again to clear the rating (toggle off).
  // ---------------------------------------------------------------------------

  // Hover highlight (mouseover/mouseout bubble; mouseenter/mouseleave don't)
  document.addEventListener('mouseover', function (e) {
    var star = e.target.closest('.star-rating:not(.star-rating--readonly) .star');
    if (!star) return;
    var container = star.closest('.star-rating');
    var value = parseInt(star.dataset.value, 10);
    container.querySelectorAll('.star').forEach(function (s, idx) {
      s.classList.toggle('star--hover', (idx + 1) <= value);
    });
  });

  document.addEventListener('mouseout', function (e) {
    var star = e.target.closest('.star-rating:not(.star-rating--readonly) .star');
    if (!star) return;
    var container = star.closest('.star-rating');
    container.querySelectorAll('.star').forEach(function (s) {
      s.classList.remove('star--hover');
    });
  });

  // Click to set / clear rating
  document.addEventListener('click', function (e) {
    var star = e.target.closest('.star-rating:not(.star-rating--readonly) .star');
    if (!star) return;
    var container = star.closest('.star-rating');
    var shopId = container.dataset.shopId;
    var clicked = parseInt(star.dataset.value, 10);
    var current = parseInt(container.dataset.rating, 10) || 0;
    // Clicking the same value again clears the rating
    var newRating = (clicked === current) ? 0 : clicked;

    fetch('/shop/' + shopId + '/rating', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating: newRating }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var rating = data.rating || 0;
        container.dataset.rating = rating;
        container.querySelectorAll('.star').forEach(function (s, idx) {
          s.classList.toggle('star--active', rating > 0 && (idx + 1) <= rating);
        });
        container.setAttribute('aria-label', rating + '星');
      })
      .catch(function (err) { console.error('rating update failed', err); });
  });

  // ---------------------------------------------------------------------------
  // Import form: confirm destructive action before submit
  // ---------------------------------------------------------------------------
  var importForm = document.getElementById('import-form');
  if (importForm) {
    importForm.addEventListener('submit', function (e) {
      var fileInput = importForm.querySelector('input[type="file"]');
      if (!fileInput || !fileInput.files.length) {
        e.preventDefault();
        alert('CSV ファイルを選択してください。');
        return;
      }
      var ok = window.confirm(
        'この操作は元に戻せません。\nデータベースを CSV の内容で上書きします。続けますか？'
      );
      if (!ok) {
        e.preventDefault();
      }
    });
  }

  // ---------------------------------------------------------------------------
  // Infinite scroll — IntersectionObserver watches #scroll-sentinel
  // Fetches /api/shops with same filter/sort params, appends rendered rows.
  // ---------------------------------------------------------------------------
  var tbody    = document.getElementById('shop-tbody');
  var sentinel = document.getElementById('scroll-sentinel');
  var loading  = document.getElementById('scroll-loading');

  if (tbody && sentinel && loading && 'IntersectionObserver' in window) {
    var hasMore   = tbody.dataset.hasMore === 'true';
    var nextPage  = parseInt(tbody.dataset.nextPage, 10) || 2;
    var fetching  = false;

    // Build query string for /api/shops, preserving current page filters/sort
    function buildApiParams(page) {
      // Clone current URL search params and override page
      var params = new URLSearchParams(window.location.search);
      params.set('page', page);
      return params.toString();
    }

    function loadNextPage() {
      if (!hasMore || fetching) return;
      fetching = true;
      loading.hidden = false;

      fetch('/api/shops?' + buildApiParams(nextPage))
        .then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function (data) {
          tbody.insertAdjacentHTML('beforeend', data.html);
          hasMore  = data.has_more;
          nextPage = data.next_page;
          fetching = false;
          loading.hidden = true;
          // Stop observing when all rows are loaded
          if (!hasMore) {
            observer.disconnect();
          }
        })
        .catch(function (err) {
          console.error('infinite scroll load failed', err);
          fetching = false;
          loading.hidden = true;
        });
    }

    var observer = new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) {
        loadNextPage();
      }
    }, { rootMargin: '200px' });

    // Only observe if there are more pages to load
    if (hasMore) {
      observer.observe(sentinel);
    }
  }
})();
