(function () {
  'use strict';
  /** @typedef {{message_id: string, message_content: string|null, discord_url: string|null, error: string, mention_ids: number[], review_mention_id: number|null, recovery_status: 'not_prepared'|'in_review'|'handled'}} IntakePost */
  /** @typedef {{items: IntakePost[], next_cursor: string|null}} IntakeResponse */
  const button = /** @type {HTMLButtonElement} */ (document.getElementById('intake-load'));
  const list = /** @type {HTMLElement} */ (document.getElementById('intake-posts'));
  const error = /** @type {HTMLElement} */ (document.getElementById('intake-error'));
  const loading = /** @type {HTMLElement} */ (document.getElementById('intake-loading'));
  const empty = /** @type {HTMLElement} */ (document.getElementById('intake-empty'));
  const token = /** @type {HTMLMetaElement} */ (document.querySelector('meta[name="csrf-token"]')).content;
  const config = /** @type {HTMLElement} */ (document.getElementById('admin-intake-config'));
  const readOnly = config.dataset.readOnly === 'true';
  /** @type {string|null} */
  let cursor = null;

  async function loadResults() {
    button.disabled = true;
    error.hidden = true;
    loading.hidden = false;
    list.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch('/api/admin/review-failures?active_only=true' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''), {cache: 'no-store'});
      if (!response.ok) throw new Error('HTTP ' + response.status);
      /** @type {IntakeResponse} */
      const data = await response.json();
      data.items.forEach(renderPost);
      cursor = data.next_cursor;
      empty.hidden = list.childElementCount > 0;
      button.hidden = !cursor;
      button.textContent = '続きを表示';
    } catch (failure) {
      console.error('Intake results load failed', {cursor, failure});
      error.textContent = '投稿一覧を取得できませんでした。再試行してください。';
      error.hidden = false;
      button.hidden = false;
      button.textContent = '再試行';
    } finally {
      loading.hidden = true;
      list.setAttribute('aria-busy', 'false');
      button.disabled = false;
    }
  }

  /** @param {IntakePost} post */
  function renderPost(post) {
    if (post.recovery_status === 'handled') return;
    const article = document.createElement('article');
    article.className = 'admin-intake-post';
    article.setAttribute('role', 'listitem');
    const sourceContent = document.createElement('div');
    sourceContent.className = 'admin-intake-source';
    const content = document.createElement('p');
    content.className = 'admin-intake-content';
    content.textContent = post.message_content || '本文を取得できなかった投稿';
    sourceContent.appendChild(content);
    if (post.discord_url) {
      const source = document.createElement('a');
      source.href = post.discord_url;
      source.target = '_blank';
      source.rel = 'noopener noreferrer';
      source.className = 'admin-intake-source-link link-secondary';
      source.textContent = '元投稿 ↗';
      source.setAttribute('aria-label', '元投稿をDiscordで開く（新しいタブ）');
      sourceContent.appendChild(source);
    }
    const reason = document.createElement('p');
    reason.className = 'admin-intake-reason';
    reason.textContent = post.error
      .replace('店舗情報の登録処理に失敗しました。', '店舗情報の登録に失敗')
      .replace('元投稿を取得できませんでした。', '元投稿の取得に失敗');
    article.append(sourceContent, reason);
    if (post.recovery_status === 'in_review') {
      if (post.review_mention_id === null) throw new Error('Missing open review item: message_id=' + post.message_id);
      const link = document.createElement('a');
      link.className = 'btn btn-outline-secondary admin-intake-action';
      link.href = '/admin/review?mention_id=' + post.review_mention_id;
      link.textContent = '内容を確認';
      article.appendChild(link);
    } else {
      const prepare = document.createElement('button');
      prepare.type = 'button';
      prepare.className = 'btn btn-outline-secondary admin-intake-action';
      prepare.textContent = '内容を確認';
      prepare.disabled = readOnly;
      if (readOnly) prepare.title = '読取専用のため、確認項目を準備できません。';
      prepare.addEventListener('click', async function () {
        prepare.disabled = true;
        prepare.textContent = '準備中…';
        error.hidden = true;
        try {
          const response = await fetch('/api/admin/review-failures/' + post.message_id + '/prepare', {method: 'POST', headers: {'X-CSRF-Token': token}});
          /** @type {{id: number, detail?: string}} */
          const data = await response.json();
          if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'HTTP ' + response.status);
          window.location.assign('/admin/review?mention_id=' + data.id);
        } catch (failure) {
          console.error('Intake recovery failed', {messageId: post.message_id, failure});
          error.textContent = '確認画面を準備できませんでした。「内容を確認」から再試行してください。';
          error.hidden = false;
          prepare.textContent = '内容を確認';
          prepare.disabled = readOnly;
        }
      });
      article.appendChild(prepare);
    }
    list.appendChild(article);
  }
  button.addEventListener('click', loadResults);
  loadResults();
})();
