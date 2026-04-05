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
