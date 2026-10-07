(() => {
  'use strict';
  const list = document.getElementById('discord-users');
  const addButton = document.getElementById('add-user-button');
  const addRow = document.getElementById('add-user');
  if (!list || !(addButton instanceof HTMLButtonElement) || !(addRow instanceof HTMLTableRowElement)) return;

  /** @param {HTMLTableRowElement} row @param {boolean} editing @returns {void} */
  function setEditing(row, editing) {
    row.classList.toggle('discord-user-row--editing', editing);
    row.querySelectorAll('[data-user-display]').forEach(control => { if (control instanceof HTMLElement) control.hidden = editing; });
    row.querySelectorAll('[data-user-editor]').forEach(control => { if (control instanceof HTMLElement) control.hidden = !editing; });
    const target = row.querySelector(editing ? 'input[type="text"]' : '[data-user-edit]');
    if (target instanceof HTMLElement) target.focus();
  }

  addButton.addEventListener('click', () => {
    addRow.hidden = false;
    addButton.setAttribute('aria-expanded', 'true');
    document.getElementById('users-empty')?.setAttribute('hidden', '');
    const input = document.getElementById('discord-user-id');
    if (input instanceof HTMLInputElement) input.focus();
  });
  list.addEventListener('click', event => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest('button');
    const row = button?.closest('.discord-user-row');
    if (!(button instanceof HTMLButtonElement) || button.disabled || !(row instanceof HTMLTableRowElement)) return;
    if (button.hasAttribute('data-user-edit')) setEditing(row, true);
    if (button.hasAttribute('data-user-cancel')) {
      const form = row.querySelector('form');
      if (form instanceof HTMLFormElement) form.reset();
      if (row === addRow) {
        addRow.hidden = true;
        addButton.setAttribute('aria-expanded', 'false');
        document.getElementById('users-empty')?.removeAttribute('hidden');
        addButton.focus();
      } else setEditing(row, false);
    }
  });
  list.addEventListener('submit', event => {
    if (!(event.target instanceof HTMLFormElement) || !(event instanceof SubmitEvent)) return;
    const submitter = event.submitter;
    if (submitter instanceof HTMLButtonElement && submitter.hasAttribute('data-user-delete')) {
      if (!window.confirm('このユーザーの利用許可を削除します。次のアクセスから利用できなくなります。')) {
        event.preventDefault();
        return;
      }
    }
    if (event.target.dataset.submitting === 'true') event.preventDefault();
    else {
      event.target.dataset.submitting = 'true';
      event.target.setAttribute('aria-busy', 'true');
    }
  });
})();
