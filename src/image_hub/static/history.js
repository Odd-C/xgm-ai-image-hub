(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const providerNames = {libtv: 'LibTV', lovart: 'Lovart', api: 'API'};
  const statusNames = {queued: '排队中', running: '生成中', succeeded: '已完成', failed: '失败', recovery_required: '需要恢复'};
  const sentimentNames = {adopted: '采用', satisfied: '满意', dissatisfied: '不满意'};
  let timer = null;
  function escapeHtml(value = '') { const div = document.createElement('div'); div.textContent = String(value); return div.innerHTML; }
  async function responseJson(response) { const data = await response.json().catch(() => ({})); if (response.status === 401) location.href = '/login'; if (!response.ok) throw new Error(data.detail || '读取失败'); return data; }
  function card(item, projectId) {
    const params = item.parameters || {}; const artifact = item.artifact_url;
    const metadata = `${providerNames[item.provider] || item.provider} · ${item.model_label} · ${params.ratio || '—'} · ${params.resolution || '—'} · ${sentimentNames[item.sentiment] || '未评价'}`;
    return `<article class="history-card" data-generation-id="${item.id}">
      ${artifact ? `<img src="${escapeHtml(artifact)}" loading="lazy" alt="生成结果">` : `<div class="result-state ${escapeHtml(item.status)}"><strong>${escapeHtml(statusNames[item.status] || item.status)}</strong></div>`}
      <div class="history-card-body"><div class="history-meta"><span title="${escapeHtml(metadata)}">${escapeHtml(metadata)}</span><span class="status-pill ${escapeHtml(item.status)}">${escapeHtml(statusNames[item.status] || item.status)}</span><time title="${new Date(item.created_at).toLocaleString('zh-CN')}">${new Date(item.created_at).toLocaleDateString('zh-CN')}</time></div><p title="${escapeHtml(item.prompt)}">${escapeHtml(item.prompt)}</p>
      <div class="history-actions">${artifact ? `<a class="secondary" href="${escapeHtml(artifact)}" target="_blank" rel="noopener">打开</a><a class="secondary" href="${escapeHtml(artifact)}?download=true" download>下载</a>` : ''}<a class="secondary" href="/projects/${projectId}?action=locate&amp;generation=${item.id}">定位画布</a><a class="primary" href="/projects/${projectId}?action=continue&amp;generation=${item.id}">继续使用</a></div></div>
    </article>`;
  }
  async function load() {
    const projectId = $('#history-project').value;
    const [provider, model] = $('#history-model').value.split('|');
    const query = new URLSearchParams({q: $('#history-search').value, provider, model, status_filter: $('#history-status').value, sentiment: $('#history-sentiment').value, limit: '100'});
    try { const data = await responseJson(await fetch(`/api/projects/${projectId}/generations?${query}`)); $('#history-count').textContent = `${data.items.length} 条记录`; $('#history-grid').innerHTML = data.items.length ? data.items.map(item => card(item, projectId)).join('') : '<p class="empty-state">没有符合条件的记录</p>'; }
    catch (error) { $('#history-grid').innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`; }
  }
  document.addEventListener('DOMContentLoaded', () => {
    $('#history-project').addEventListener('change', event => { location.href = `/projects/${event.target.value}/history`; });
    ['#history-status','#history-model','#history-sentiment'].forEach(selector => $(selector).addEventListener('change', load));
    $('#history-search').addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 250); });
    load();
  });
})();
