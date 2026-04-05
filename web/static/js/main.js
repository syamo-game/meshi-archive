// Meshi Archive — minimal progressive enhancement
// All features work without JS. JS adds convenience only.

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // Auto-submit filter form on select / radio change
  // This avoids requiring the user to click "絞り込む" for simple changes.
  // ---------------------------------------------------------------------------
  const filterForm = document.getElementById('filter-form');
  if (filterForm) {
    filterForm.querySelectorAll('select, input[type="radio"]').forEach(function (el) {
      el.addEventListener('change', function () {
        filterForm.submit();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // Visited toggle — POST /shop/{id}/visited, update badge in place
  // ---------------------------------------------------------------------------
  document.querySelectorAll('.btn-visit-toggle').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var shopId = btn.dataset.shopId;
      fetch('/shop/' + shopId + '/visited', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.dataset.visited = data.is_visited ? 'true' : 'false';
          btn.textContent = data.is_visited ? '訪問済み' : '未訪問';
          btn.classList.toggle('badge--visited', data.is_visited);
          btn.classList.toggle('badge--unvisited', !data.is_visited);
          var date = data.visited_at || '未訪問';
          btn.title = data.is_visited ? '訪問日: ' + date : '未訪問';
        })
        .catch(function (err) { console.error('visited toggle failed', err); });
    });
  });

  // ---------------------------------------------------------------------------
  // Star rating — POST /shop/{id}/rating with {rating: n}
  // Clicking the active star clears rating (toggle off).
  // ---------------------------------------------------------------------------
  document.querySelectorAll('.star-rating').forEach(function (container) {
    var stars = container.querySelectorAll('.star');

    // Hover: highlight stars up to hovered index
    stars.forEach(function (star) {
      star.addEventListener('mouseenter', function () {
        var value = parseInt(star.dataset.value, 10);
        stars.forEach(function (s, idx) {
          s.classList.toggle('star--hover', (idx + 1) <= value);
        });
      });
      star.addEventListener('mouseleave', function () {
        stars.forEach(function (s) { s.classList.remove('star--hover'); });
      });
    });

    // Click: send to API and update display
    stars.forEach(function (star) {
      star.addEventListener('click', function () {
        var shopId = container.dataset.shopId;
        var clicked = parseInt(star.dataset.value, 10);
        var current = parseInt(container.dataset.rating, 10) || 0;
        // Clicking the same star again clears the rating
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
            stars.forEach(function (s, idx) {
              s.classList.toggle('star--active', rating > 0 && (idx + 1) <= rating);
            });
            container.setAttribute('aria-label', rating + '星');
          })
          .catch(function (err) { console.error('rating update failed', err); });
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Import form: confirm destructive action before submit
  // ---------------------------------------------------------------------------
  const importForm = document.getElementById('import-form');
  if (importForm) {
    importForm.addEventListener('submit', function (e) {
      const fileInput = importForm.querySelector('input[type="file"]');
      if (!fileInput || !fileInput.files.length) {
        e.preventDefault();
        alert('CSV ファイルを選択してください。');
        return;
      }
      const ok = window.confirm(
        'この操作は元に戻せません。\nデータベースを CSV の内容で上書きします。続けますか？'
      );
      if (!ok) {
        e.preventDefault();
      }
    });
  }
})();
