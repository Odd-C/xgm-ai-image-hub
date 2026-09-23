(() => {
  'use strict';
  const csrfHeaders = { 'X-CSRF-Token': window.ASSET_CSRF || '' };
  function toast(message, error = false) {
    let el = document.getElementById('toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; document.body.appendChild(el); }
    el.textContent = message;
    el.className = 'toast show' + (error ? ' error' : '');
    window.setTimeout(() => { el.className = 'toast'; }, 2400);
  }
  async function responseJson(response) {
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) location.href = '/login';
    if (!response.ok) throw new Error(payload.detail || '操作失败');
    return payload;
  }
  const upload = document.getElementById('asset-upload');
  if (upload) {
    upload.addEventListener('change', async () => {
      const files = [...upload.files];
      if (!files.length) return;
      upload.value = '';
      let succeeded = 0;
      for (const file of files) {
        const form = new FormData();
        form.append('file', file);
        try {
          await responseJson(await fetch('/api/assets', {method: 'POST', headers: csrfHeaders, body: form}));
          succeeded += 1;
        } catch (error) {
          toast(`「${file.name || '未命名图片'}」上传失败：${error.message}`, true);
        }
      }
      if (succeeded) location.reload();
    });
  }
  const grid = document.getElementById('asset-grid');
  if (grid) {
    /* The empty note lives in the grid permanently; deleting only ever removes
       cards, so keep the note in step with what is actually left. */
    const empty = document.getElementById('asset-empty');
    const syncEmpty = () => { if (empty) empty.hidden = grid.querySelector('.asset-card') !== null; };
    grid.addEventListener('click', async event => {
      const button = event.target.closest('.asset-delete');
      if (!button) return;
      const id = button.dataset.assetId;
      if (!id) return;
      button.disabled = true;
      try {
        await responseJson(await fetch(`/api/assets/${id}/delete`, {method: 'POST', headers: csrfHeaders}));
        const card = grid.querySelector(`.asset-card[data-asset-id="${id}"]`);
        if (card) card.remove();
        syncEmpty();
        toast('已删除素材');
      } catch (error) {
        button.disabled = false;
        toast(`删除失败：${error.message}`, true);
      }
    });
  }
})();
