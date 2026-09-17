(() => {
  'use strict';

  const models = window.IMAGE_HUB_MODELS || [];
  const core = window.ImageHubCanvasCore;
  if (!core) throw new Error('画布核心模块未加载');
  const projectId = window.IMAGE_HUB_PROJECT_ID || '';
  const csrfHeaders = {'X-CSRF-Token': window.IMAGE_HUB_CSRF || ''};
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const providerNames = {libtv: 'LibTV', lovart: 'Lovart', api: 'API'};
  const statusNames = {queued: '排队中', running: '生成中', succeeded: '已完成', failed: '失败', recovery_required: '需要恢复'};
  const sentimentNames = {adopted: '采用', satisfied: '满意', dissatisfied: '不满意'};
  const acceptedImageTypes = new Set(['image/png', 'image/jpeg', 'image/webp']);
  const maxLocalImageBytes = 30 * 1024 * 1024;
  const imageOffset = 32;
  const resultClickDelay = 220;
  const urlOnlyMessage = '暂不支持仅粘贴图片链接，请复制图片本身或下载后拖入';
  /* Two selection modes exist and never overlap: `selectedIds` holds canvas
     nodes, `selectedLinks` holds input connections. Keeping them exclusive is
     what makes `Delete` unambiguous. A link key is `target::source`, which is
     unique because one card can reference the same input only once. */
  const state = {version: core.SCHEMA_VERSION, viewport: {x: 0, y: 0, zoom: 1}, nodes: [], selectedIds: new Set(), selectedLinks: new Set(), connecting: null};
  const localFiles = new Map();
  const objectUrls = new Map();
  const extensionHooks = new Set();
  let canvasClipboard = null;
  let saveTimer = null;
  let pollTimer = null;
  let drag = null;
  let pan = null;
  let minimapDrag = null;
  let minimapGeometry = null;
  let resizeObserver = null;
  let renderFrame = 0;
  let linkFrame = 0;
  let resultClickTimer = null;
  let pendingUploadPoint = null;
  let activePickerId = '';
  let activePickerKind = '';
  const pickerProviders = new Map();
  let context = null;
  let escapeArmed = false;
  let suppressClick = false;
  let pointerSelectionId = '';
  let lastCanvasPointer = null;
  const viewerState = {scale: 1, panX: 0, panY: 0, fit: true, pointerId: null, startX: 0, startY: 0, originX: 0, originY: 0, invoker: null};

  window.ImageHubResultActions = Object.freeze({register(handler) { if (typeof handler !== 'function') throw new TypeError('result action hook must be a function'); extensionHooks.add(handler); return () => extensionHooks.delete(handler); }});

  function emitResult(node, result, event = 'refresh') {
    const detail = Object.freeze({event, generationId: result.generationId || result.id, artifactUrl: result.artifactUrl || '', prompt: result.prompt || node.prompt || '', provider: result.provider || node.provider || '', model: result.modelLabel || '', parameters: Object.freeze({...result.parameters}), sentiment: result.sentiment || ''});
    extensionHooks.forEach(handler => { try { handler(detail); } catch (error) { console.warn('结果扩展处理失败', error); } });
    window.dispatchEvent(new CustomEvent('imagehub:result-action', {detail}));
  }
  function uid(prefix) { const raw = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`; return `${prefix}-${raw.replace(/[^a-z0-9]/gi, '')}`; }
  function escapeHtml(value = '') { const div = document.createElement('div'); div.textContent = String(value); return div.innerHTML; }
  const icons = Object.freeze({
    open: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5h5v5M19 5l-8 8"/><path d="M17 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h5"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m-4-4 4 4 4-4M5 19h14"/></svg>',
    remove: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/></svg>',
    chevron: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>'
  });
  function downloadUrl(url) { if (!url) return ''; return `${url}${url.includes('?') ? '&' : '?'}download=true`; }
  function downloadFileName(disposition, url) {
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(String(disposition || ''));
    if (match) {
      let name = match[1].trim();
      try { name = decodeURIComponent(name); } catch { /* keep the raw header value */ }
      if (name.toLowerCase().endsWith('.png')) return name;
    }
    const tail = String(url || '').split('?')[0].split('/').filter(Boolean).pop() || 'xgm-ai-image-hub';
    return `${tail.replace(/\.[a-z0-9]+$/i, '') || 'xgm-ai-image-hub'}.png`;
  }
  function toast(message, error = false) { const el = $('#toast'); el.textContent = message; el.className = `toast show${error ? ' error' : ''}`; window.setTimeout(() => { el.className = 'toast'; }, 2400); }
  async function responseJson(response) { const payload = await response.json().catch(() => ({})); if (response.status === 401) location.href = '/login'; if (!response.ok) throw new Error(payload.detail || '操作失败'); return payload; }
  function profileById(id) { return core.profileById(models, id); }
  function providerLabel(value) { return providerNames[value] || value || '平台'; }
  function comboValue(ratio, resolution) { return `${ratio}|${resolution}`; }
  /* The server sends a per-ratio tier map for Lovart models (some ratios have no
     4K). Fall back to the flat resolution list for providers that do not. */
  function profileTiers(profile, ratio) {
    if (!profile) return [];
    const mapped = profile.tiers?.[ratio];
    return Array.isArray(mapped) && mapped.length ? mapped : (profile.resolutions || []);
  }
  function combos(profile) { return profile ? profile.ratios.flatMap(ratio => profileTiers(profile, ratio).map(resolution => ({ratio, resolution, value: comboValue(ratio, resolution)}))) : []; }

  function nodeById(id) { return state.nodes.find(node => node.id === id); }
  function generationNodes() { return state.nodes.filter(core.isGenerationNode); }
  function worldPoint(clientX, clientY) { const rect = $('#canvas-viewport').getBoundingClientRect(); return {x: (clientX - rect.left - state.viewport.x) / state.viewport.zoom, y: (clientY - rect.top - state.viewport.y) / state.viewport.zoom}; }
  function serializeNode(node) { const copy = {...node}; if (copy.localOnly) copy.src = ''; delete copy.uploading; delete copy.submitting; delete copy.dirty; return copy; }

  function scheduleSave() { $('#save-state').innerHTML = '<i></i>保存中'; clearTimeout(saveTimer); saveTimer = setTimeout(saveCanvas, 450); }
  async function saveCanvas() {
    try {
      await responseJson(await fetch(`/api/projects/${projectId}/canvas`, {method: 'PUT', headers: {'Content-Type': 'application/json', ...csrfHeaders}, body: JSON.stringify({version: core.SCHEMA_VERSION, viewport: state.viewport, nodes: state.nodes.map(serializeNode)})}));
      $('#save-state').innerHTML = '<i></i>已保存';
    } catch (error) { $('#save-state').innerHTML = '<i class="error"></i>保存失败'; console.warn('画布保存失败', error); }
  }

  /* ---- unified generation node ------------------------------------------- */

  function defaultGeneration(x, y, inherited = {}) {
    const profile = core.chooseRequestProfile(models, inherited.profileId || '');
    const firstCombo = combos(profile)[0] || {ratio: '1:1', resolution: '2K'};
    const compatible = combos(profile).find(item => item.ratio === inherited.ratio && item.resolution === inherited.resolution) || firstCombo;
    return core.normalizeGenerationNode({
      id: uid('generation'),
      type: core.GENERATION,
      x, y, width: core.DEFAULT_WIDTH,
      expanded: inherited.expanded !== false,
      prompt: inherited.prompt || '',
      provider: profile?.provider || inherited.provider || '',
      profileId: profile?.id || inherited.profileId || '',
      ratio: compatible.ratio,
      resolution: compatible.resolution,
      quality: inherited.quality || profile?.qualities?.[0] || 'standard',
      count: core.clampCount(inherited.count),
      orderedInputIds: inherited.orderedInputIds ? [...inherited.orderedInputIds] : (inherited.inputId ? [inherited.inputId] : []),
      parentGenerationId: inherited.parentGenerationId || '',
      profileUnavailable: Boolean(inherited.profileId && !profile),
    });
  }
  /* Every path that mutates the node collection must redraw explicitly:
     selectNode() is presentation-only and no longer carries the implicit
     redraw that used to make a new card appear without a page reload. */
  function addGeneration(x, y, inherited = {}) { const node = defaultGeneration(x, y, inherited); state.nodes.push(node); selectNode(node.id, false, false); render(); scheduleLayoutRecompute(); scheduleSave(); if (node.expanded) requestAnimationFrame(() => $(`[data-node-id="${node.id}"] textarea`)?.focus()); return node; }
  function addInput(nodeId, ref) {
    const node = nodeById(nodeId);
    if (!core.isGenerationNode(node)) return false;
    const parsed = core.parseResultRefId(ref);
    if (parsed && parsed.nodeId === nodeId) { toast('不能引用本节点自己的结果', true); return false; }
    if (!core.resolveInputRef(state.nodes, ref)) return false;
    node.orderedInputIds ||= [];
    if (node.orderedInputIds.includes(ref)) return false;
    node.orderedInputIds.push(ref);
    syncDirty(node);
    render(); scheduleSave();
    return true;
  }

  function removeIds(ids) {
    const removed = new Set(ids);
    removed.forEach(id => { if (objectUrls.has(id)) URL.revokeObjectURL(objectUrls.get(id)); objectUrls.delete(id); localFiles.delete(id); });
    state.nodes = core.removePresentationNodes(state.nodes, removed);
    state.selectedIds = new Set([...state.selectedIds].filter(id => !removed.has(id)));
    state.selectedLinks = new Set(liveLinkKeys().filter(key => !removed.has(linkTarget(key))));
    render(); scheduleSave();
  }
  function syncSelectionClasses() {
    $$('.canvas-node').forEach(element => {
      const selected = state.selectedIds.has(element.dataset.nodeId);
      element.classList.toggle('selected', selected);
      element.setAttribute('aria-selected', String(selected));
    });
  }

  /* ---- input connection selection ---------------------------------------
     One connection equals one entry of the target card's `orderedInputIds`,
     which is the same relationship the reference-thumbnail 「×」 button edits.
     Selecting a link never touches node selection and vice versa, so `Delete`
     has exactly one meaning at a time. */
  function linkKey(source, target) { return `${target}::${source}`; }
  function linkTarget(key) { const at = key.indexOf('::'); return at < 0 ? '' : key.slice(0, at); }
  function linkSource(key) { const at = key.indexOf('::'); return at < 0 ? '' : key.slice(at + 2); }
  function liveLinkKeys() {
    return [...state.selectedLinks].filter(key => {
      const target = nodeById(linkTarget(key));
      return core.isGenerationNode(target) && (target.orderedInputIds || []).includes(linkSource(key));
    });
  }
  /* Presentation-only, exactly like syncSelectionClasses: toggling a class
     avoids re-rendering the SVG under the pointer. */
  function syncLinkSelectionClasses() {
    $$('#canvas-links .input-link, #canvas-links .link-hit').forEach(element => element.classList.toggle('selected', state.selectedLinks.has(linkKey(element.dataset.source, element.dataset.target))));
  }
  function clearLinkSelection() {
    if (!state.selectedLinks.size) return false;
    state.selectedLinks.clear(); syncLinkSelectionClasses(); return true;
  }
  function selectLink(source, target, additive = false) {
    if (!source || !target) return;
    const key = linkKey(source, target);
    /* Only node selection is dropped here; the link set itself is what an
       additive Shift click extends. */
    if (state.selectedIds.size) { state.selectedIds.clear(); syncSelectionClasses(); }
    if (additive) { if (state.selectedLinks.has(key)) state.selectedLinks.delete(key); else state.selectedLinks.add(key); }
    else state.selectedLinks = new Set([key]);
    syncLinkSelectionClasses();
  }
  /* Selection stays a presentation-only DOM update so a single click never
     re-renders the node under the pointer (double click must survive). */
  function selectNode(id, additive = false, focus = true) {
    const node = nodeById(id); if (!node) return;
    clearLinkSelection();
    if (additive) { if (state.selectedIds.has(id)) state.selectedIds.delete(id); else state.selectedIds.add(id); }
    else state.selectedIds = new Set([id]);
    syncSelectionClasses();
    if (focus) requestAnimationFrame(() => $(`[data-node-id="${id}"]`)?.focus({preventScroll: true}));
  }
  function clearSelection() { const clearedLinks = clearLinkSelection(); if (!state.selectedIds.size) return clearedLinks; state.selectedIds.clear(); syncSelectionClasses(); return true; }

  /* Removing a connection drops that one reference from the target card's
     ordered input list. Everything that is not that exact entry keeps its
     relative position, and uploads, results and Generations are untouched.
     A key whose reference is already gone is a silent no-op. */
  function removeLinks(keys) {
    const byTarget = new Map();
    keys.forEach(key => {
      const target = linkTarget(key); const source = linkSource(key);
      if (!target || !source) return;
      if (!byTarget.has(target)) byTarget.set(target, new Set());
      byTarget.get(target).add(source);
    });
    let removed = 0;
    byTarget.forEach((sources, targetId) => {
      const node = nodeById(targetId);
      if (!core.isGenerationNode(node) || !Array.isArray(node.orderedInputIds)) return;
      const next = node.orderedInputIds.filter(ref => { if (!sources.has(ref)) return true; removed += 1; return false; });
      if (next.length === node.orderedInputIds.length) return;
      node.orderedInputIds = next; syncDirty(node);
    });
    if (!removed) return 0;
    state.selectedLinks.clear();
    render(); scheduleSave();
    toast(removed === 1 ? '已移除 1 条连线' : `已移除 ${removed} 条连线`);
    return removed;
  }

  function copySelection() {
    if (!state.selectedIds.size) return false;
    canvasClipboard = {nodes: state.nodes.map(node => structuredClone(serializeNode(node))), selectedIds: [...state.selectedIds]};
    toast(`已复制 ${state.selectedIds.size} 个画布节点`); return true;
  }
  function pasteClipboard(point) {
    if (!canvasClipboard?.selectedIds.length) return false;
    const selected = canvasClipboard.nodes.filter(node => canvasClipboard.selectedIds.includes(node.id));
    const minX = Math.min(...selected.map(node => node.x)); const minY = Math.min(...selected.map(node => node.y));
    const viewport = $('#canvas-viewport').getBoundingClientRect();
    const target = point || worldPoint(viewport.left + viewport.width / 2, viewport.top + viewport.height / 2);
    const outcome = core.clonePresentationNodes(canvasClipboard.nodes, canvasClipboard.selectedIds, uid, {x: target.x - minX + 24, y: target.y - minY + 24});
    outcome.clones.forEach(clone => {
      const oldId = Object.keys(outcome.idMap).find(id => outcome.idMap[id] === clone.id);
      if (clone.type === core.IMAGE && oldId && localFiles.has(oldId)) { const file = localFiles.get(oldId); const src = URL.createObjectURL(file); localFiles.set(clone.id, file); objectUrls.set(clone.id, src); clone.src = src; clone.needsReselect = false; }
    });
    state.nodes.push(...outcome.clones); state.selectedIds = new Set(outcome.clones.map(node => node.id)); render(); scheduleSave(); toast(`已粘贴 ${outcome.clones.length} 个节点`); return true;
  }

  function pickerOpen(node, kind) { return activePickerId === node.id && activePickerKind === kind; }
  function ratioPreview(ratio) { return `<span class="ratio-preview" style="--ratio:${escapeHtml(ratio.replace(':', '/'))}" aria-hidden="true"></span>`; }
  function modelPickerMarkup(node, profile) {
    const grouped = models.reduce((map, item) => { (map[item.provider] ||= []).push(item); return map; }, {});
    const selectedProvider = pickerProviders.get(node.id) || profile?.provider || Object.keys(grouped)[0] || '';
    const label = profile ? `${providerLabel(profile.provider)} · ${profile.label}` : '请选择平台与模型';
    const isOpen = pickerOpen(node, 'model');
    const visible = grouped[selectedProvider] || [];
    return `<div class="visual-picker model-picker"><button type="button" class="selector-trigger" data-model-trigger aria-expanded="${isOpen}" aria-haspopup="dialog" aria-controls="models-${node.id}"><span>${escapeHtml(label)}</span>${icons.chevron}</button><div class="selector-popover" id="models-${node.id}" role="dialog" aria-label="平台与模型" ${isOpen ? '' : 'hidden'}><header><strong>平台与模型</strong><button type="button" data-close-picker aria-label="关闭平台与模型">${icons.close}</button></header><section class="selector-section"><span>平台</span><div class="provider-chips" role="tablist" aria-label="平台">${Object.keys(grouped).map(provider => `<button type="button" role="tab" data-provider-option="${escapeHtml(provider)}" aria-selected="${provider === selectedProvider}" tabindex="${provider === selectedProvider ? '0' : '-1'}">${escapeHtml(providerLabel(provider))}</button>`).join('')}</div></section><section class="selector-section"><span>模型</span><div class="model-card-grid" role="radiogroup" aria-label="模型">${visible.map(item => `<button type="button" role="radio" data-profile-option="${escapeHtml(item.id)}" aria-checked="${item.id === node.profileId}" ${item.enabled ? '' : 'disabled'}><span>${escapeHtml(item.label)}</span><small>${item.enabled ? (item.id === node.profileId ? '已选择' : '可用') : '未配置'}</small>${item.id === node.profileId ? '<b aria-hidden="true">✓</b>' : ''}</button>`).join('')}</div></section></div></div>`;
  }
  function imageSettingsPickerMarkup(node, profile) {
    const isOpen = pickerOpen(node, 'image');
    /* The resolution chips reflect the *currently selected ratio*: a ratio the
       upstream only publishes at 1K/2K must not offer a 4K button. */
    const tiers = profileTiers(profile, node.ratio);
    return `<div class="visual-picker image-settings-picker"><button type="button" class="selector-trigger" data-image-settings-trigger aria-expanded="${isOpen}" aria-haspopup="dialog" aria-controls="image-settings-${node.id}"><span>${escapeHtml(node.ratio)} · ${escapeHtml(node.resolution)}</span>${icons.chevron}</button><div class="selector-popover" id="image-settings-${node.id}" role="dialog" aria-label="图像设置" ${isOpen ? '' : 'hidden'}><header><strong>图像设置</strong><button type="button" data-close-picker aria-label="关闭图像设置">${icons.close}</button></header><section class="selector-section"><span>分辨率</span><div class="resolution-chips" role="radiogroup" aria-label="分辨率">${tiers.map(resolution => `<button type="button" role="radio" data-resolution-option="${escapeHtml(resolution)}" aria-checked="${resolution === node.resolution}">${escapeHtml(resolution)}</button>`).join('')}</div></section><section class="selector-section"><span>宽高比</span><div class="ratio-card-grid" role="radiogroup" aria-label="宽高比">${(profile?.ratios || []).map(ratio => `<button type="button" role="radio" data-ratio-option="${escapeHtml(ratio)}" aria-checked="${ratio === node.ratio}">${ratioPreview(ratio)}<span>${escapeHtml(ratio)}</span></button>`).join('')}</div></section></div></div>`;
  }

  function syncDirty(node) { const next = core.isDirty(node); const changed = next !== node.dirty; node.dirty = next; if (changed) syncDirtyUi(node); return changed; }
  function syncDirtyUi(node) {
    const element = $(`[data-node-id="${node.id}"]`); if (!element) return;
    element.classList.toggle('dirty', node.dirty);
    $('[data-dirty-flag]', element)?.toggleAttribute('hidden', !node.dirty);
  }
  function displayBatch(node) {
    if (core.batchResults(node.activeBatch).length) return node.activeBatch;
    if (core.batchResults(node.attempt).length) return node.attempt;
    return null;
  }
  function generationStatusText(node) {
    const status = core.nodeStatus(node);
    const results = core.batchResults(node.activeBatch);
    if (status === 'processing') return '生成中';
    if (status === 'empty') return '待生成';
    if (status === 'succeeded') return `已完成 ${results.length} 张`;
    if (status === 'partial') return `已完成 ${results.filter(result => result.status === 'succeeded').length}/${results.length} 张`;
    if (status === 'recovery_required') return '需要恢复';
    return '生成失败';
  }
  function primaryOf(node) {
    const batch = displayBatch(node);
    if (!batch) return null;
    const results = core.batchResults(batch);
    return results.find(result => result.id === node.primaryResultId)
      || results.find(result => result.status === 'succeeded')
      || results[0];
  }
  /* Regeneration lineage always points at the explicit primary image of the
     active batch, never at a failed attempt or a DOM position. */
  function lineageParentId(node) {
    const results = core.batchResults(node.activeBatch);
    const primary = results.find(result => result.id === node.primaryResultId)
      || results.find(result => result.status === 'succeeded');
    return (primary?.status === 'succeeded' ? primary.generationId : '') || node.parentGenerationId || '';
  }
  function resultTileMarkup(node, result, primaryId, showPrimaryTag) {
    const succeeded = result.status === 'succeeded' && Boolean(result.artifactUrl);
    const failed = ['failed', 'recovery_required'].includes(result.status);
    const media = succeeded
      ? `<img src="${escapeHtml(result.artifactUrl)}" alt="生成结果" loading="lazy">`
      : `<div class="result-state ${escapeHtml(result.status)}"><span class="status-spinner"></span><strong>${escapeHtml(statusNames[result.status] || '生成中')}</strong>${result.error ? `<small>${escapeHtml(result.error)}</small>` : ''}${failed && result.canRetry && result.generationId ? `<button class="retry-action" type="button" data-retry-generation="${escapeHtml(result.generationId)}">安全重试</button>` : ''}</div>`;
    const isPrimary = Boolean(primaryId) && primaryId === result.id;
    return `<div class="result-tile${isPrimary ? ' primary' : ''}${succeeded ? ' ready' : ' pending'}" data-result-id="${escapeHtml(result.id)}">${media}${isPrimary && showPrimaryTag ? '<span class="primary-tag">主图</span>' : ''}${succeeded ? `<button class="tile-port" type="button" tabindex="-1" data-tile-ref="${escapeHtml(core.resultRefId(node.id, result.id))}" aria-label="从此结果创建连接"></button>` : ''}</div>`;
  }
  function resultGridMarkup(node, batch, isActiveBatch) {
    const results = core.batchResults(batch);
    const single = results.length === 1;
    const primaryId = isActiveBatch ? primaryOf(node)?.id || '' : '';
    const showPrimaryTag = results.length > 1;
    const style = single ? ` style="--media-ratio:${escapeHtml(results[0].aspect || '4/3')}"` : '';
    return `<div class="result-grid${single ? ' single' : ''}" data-count="${results.length}"${style}>${results.map(result => resultTileMarkup(node, result, primaryId, showPrimaryTag)).join('')}</div>`;
  }
  function batchProgressMarkup(node) {
    const busy = node.submitting || (node.attempt && !node.attempt.settled);
    if (!busy) return '';
    const progress = core.batchProgress(node);
    const done = node.submitting && !progress?.done ? 0 : progress?.done || 0;
    const total = progress?.total || core.clampCount(node.count);
    const keeping = core.batchResults(node.activeBatch).length ? ' · 保留上一版图片' : '';
    const label = node.submitting && !progress?.done ? '正在提交任务' : `生成中 ${done}/${total}${keeping}`;
    return `<div class="batch-progress" role="status"><span class="status-spinner"></span><span>${escapeHtml(label)}</span><i class="progress-track" aria-hidden="true"><b style="width:${Math.round(done / Math.max(1, total) * 100)}%"></b></i></div>`;
  }
  function generationErrorMarkup(node, batch) {
    if (!node.error || core.nodeStatus(node) === 'processing') return '';
    const retries = batch === node.attempt || !node.attempt
      ? ''
      : core.batchResults(node.attempt).filter(result => result.canRetry && result.generationId).map(result => `<button class="retry-action" type="button" data-retry-generation="${escapeHtml(result.generationId)}">安全重试</button>`).join('');
    return `<p class="generation-error" role="status"><span>${escapeHtml(node.error)}</span>${retries}</p>`;
  }
  function inputRowsMarkup(node) {
    return (node.orderedInputIds || []).map((ref, index) => {
      const source = core.resolveInputRef(state.nodes, ref);
      if (!source) return `<li class="missing" data-input-id="${escapeHtml(ref)}" data-request-id="${node.id}"><span>图${index + 1} 已失效</span><button type="button" data-remove-input="${escapeHtml(ref)}" aria-label="移除图${index + 1}">×</button></li>`;
      return `<li draggable="true" data-input-id="${escapeHtml(ref)}" data-request-id="${node.id}"><span class="input-index">图${index + 1}</span><img src="${escapeHtml(source.src)}" alt=""><span class="drag-label">拖动排序</span><button type="button" data-remove-input="${escapeHtml(ref)}" aria-label="移除图${index + 1}">×</button></li>`;
    }).join('');
  }
  function sentimentRowMarkup(node) {
    const primary = primaryOf(node);
    if (!primary || primary.status !== 'succeeded' || !primary.generationId) return '';
    return `<div class="sentiment-row"><span>主图评价</span><div class="sentiment-group" role="group" aria-label="主图评价">${Object.entries(sentimentNames).map(([key, label]) => `<button type="button" data-sentiment="${key}" class="${primary.sentiment === key ? 'selected' : ''}" aria-pressed="${primary.sentiment === key}">${label}</button>`).join('')}</div></div>`;
  }
  function generationEditorMarkup(node) {
    const profile = profileById(node.profileId);
    const availability = core.requestAvailability(profile);
    const quality = profile?.qualities?.length > 1 ? `<details class="more-settings"><summary>更多设置</summary><label>质量<select data-field="quality">${profile.qualities.map(value => `<option ${value === node.quality ? 'selected' : ''}>${escapeHtml(value)}</option>`).join('')}</select></label></details>` : '';
    const rows = inputRowsMarkup(node);
    const busy = node.submitting || Boolean(node.attempt && !node.attempt.settled);
    const hasBatch = Boolean(displayBatch(node));
    const label = node.submitting ? '正在提交…' : busy ? '生成中…' : hasBatch ? '重新生成' : '生成图片';
    return `<div class="generation-editor">${rows ? `<ol class="request-inputs">${rows}</ol>` : '<p class="no-inputs">无参考图，可直接文生图</p>'}<label class="prompt-label">提示词<textarea data-field="prompt" maxlength="12000" required placeholder="描述要生成或修改的画面">${escapeHtml(node.prompt)}</textarea></label><label>平台 · 模型${modelPickerMarkup(node, profile)}</label><div class="request-settings"><label>比例 · 分辨率${imageSettingsPickerMarkup(node, profile)}</label><label>数量<select data-field="count">${[1, 2, 3, 4].map(value => `<option value="${value}" ${value === Number(node.count) ? 'selected' : ''}>${value} 张</option>`).join('')}</select></label></div>${quality}${sentimentRowMarkup(node)}${availability.enabled ? '' : `<p class="unavailable-model" role="status">${escapeHtml(availability.message)}</p>`}<button class="generate-button" type="button" data-generate ${availability.enabled && !busy ? '' : 'disabled'}>${label}</button></div>`;
  }
  /* The rail owns the single result-count readout; the header only reports the
     input count (plus a status while no result summary exists yet). The card's
     only delete control lives in the header, and expanding the parameters is
     owned by a single click on the result (plus the header chevron for the
     keyboard path), so the rail keeps just the readout and the two file
     actions: the readout takes the free space and the actions sit flush right,
     which keeps the footer balanced without an empty slot. */
  function generationRailMarkup(node) {
    const batch = displayBatch(node);
    if (!batch) return '';
    const results = core.batchResults(batch);
    const primary = primaryOf(node);
    const ready = Boolean(primary && primary.status === 'succeeded' && primary.artifactUrl);
    const succeeded = results.filter(result => result.status === 'succeeded').length;
    const meta = `${succeeded}/${results.length} 张结果${node.error ? ' · 有失败' : ''}`;
    return `<footer class="generation-rail"><span class="rail-meta" title="${escapeHtml(meta)}">${escapeHtml(meta)}</span><div class="node-action-rail"><div class="file-actions"><button type="button" data-open-result ${ready ? '' : 'disabled'} aria-label="打开主图" title="打开主图">${icons.open}</button><button type="button" data-download-result ${ready ? '' : 'disabled'} aria-label="下载主图" title="下载主图">${icons.download}</button></div></div></footer>`;
  }
  function generationMarkup(node) {
    const inputs = (node.orderedInputIds || []).length;
    const activeBatch = core.batchResults(node.activeBatch).length ? node.activeBatch : null;
    const batch = activeBatch || (core.batchResults(node.attempt).length ? node.attempt : null);
    const expanded = Boolean(node.expanded);
    const media = batch ? resultGridMarkup(node, batch, Boolean(activeBatch)) : '';
    const inputMeta = `${inputs} 张输入${batch ? '' : ` · ${escapeHtml(generationStatusText(node))}`}`;
    const classes = ['canvas-node', 'generation-node', expanded ? 'expanded' : '', batch ? 'has-results' : '', state.selectedIds.has(node.id) ? 'selected' : '', node.dirty ? 'dirty' : ''].filter(Boolean).join(' ');
    return `<article class="${classes}" data-node-id="${node.id}" data-node-type="${core.GENERATION}" aria-selected="${state.selectedIds.has(node.id)}" tabindex="-1" style="left:${node.x}px;top:${node.y}px;width:${node.width}px"><button class="input-port" type="button" aria-label="参考图输入端口"></button><header class="node-header"><div class="generation-heading"><span>生成</span><small>${inputMeta}</small><em class="dirty-flag" data-dirty-flag title="已修改，待重新生成；当前图片会保留。" ${node.dirty ? '' : 'hidden'}>待重新生成</em></div><div class="node-header-actions"><button type="button" data-toggle-generation aria-label="${expanded ? '收起为结果简洁态' : '展开编辑参数'}" title="${expanded ? '收起为结果简洁态' : '展开编辑参数'}" aria-expanded="${expanded}">${icons.chevron}</button><span class="request-action-divider" aria-hidden="true"></span><button type="button" class="request-remove" data-delete-node aria-label="从画布移除" title="从画布移除">${icons.remove}</button></div></header><div class="generation-body"><div class="generation-media">${media}${batchProgressMarkup(node)}${generationErrorMarkup(node, batch)}</div>${expanded ? generationEditorMarkup(node) : ''}</div>${generationRailMarkup(node)}</article>`;
  }
  function imageMarkup(node) {
    const needsReselect = node.needsReselect || (node.localOnly && !localFiles.has(node.id));
    const available = node.src || '';
    const media = needsReselect ? '<div class="result-state upload-missing"><strong>需要重新选择图片</strong><small>刷新后需重新授权本地文件</small><button type="button" data-reselect-image>重新选择图片</button></div>' : `<img src="${escapeHtml(available)}" alt="${escapeHtml(node.name || '参考图片')}">`;
    const dimensions = node.naturalWidth && node.naturalHeight ? ` · ${node.naturalWidth} × ${node.naturalHeight}` : '';
    const label = `${node.name || '参考图片'}${dimensions}`;
    const openAction = available && !needsReselect ? `<button type="button" data-open-image aria-label="打开原图" title="打开原图">${icons.open}</button>` : '';
    return `<article class="canvas-node image-node${state.selectedIds.has(node.id) ? ' selected' : ''}" data-node-id="${node.id}" data-node-type="${core.IMAGE}" aria-selected="${state.selectedIds.has(node.id)}" tabindex="-1" style="left:${node.x}px;top:${node.y}px;width:${node.width}px"><div class="image-frame" style="aspect-ratio:${node.aspect || '4/3'}">${media}</div><footer class="image-node-bar"><span title="${escapeHtml(label)}">${escapeHtml(label)}</span><div class="node-action-rail"><div class="file-actions">${openAction}</div><div class="remove-actions"><button type="button" data-delete-node aria-label="从画布移除" title="从画布移除">${icons.remove}</button></div></div></footer><button class="output-port" type="button" aria-label="从这张图片创建连接"></button></article>`;
  }

  function curvePath(a, b) { const bend = Math.max(70, Math.abs(b.x - a.x) * .45); return `M ${a.x} ${a.y} C ${a.x + bend} ${a.y}, ${b.x - bend} ${b.y}, ${b.x} ${b.y}`; }
  function portWorld(nodeId, selector) { const port = $(`[data-node-id="${nodeId}"] ${selector}`); const viewport = $('#canvas-viewport'); if (!port || !viewport) return null; return core.rectCenterToWorld(port.getBoundingClientRect(), viewport.getBoundingClientRect(), state.viewport); }
  /* A 1.25px stroke is unhittable with a mouse, so every connection renders a
     second, invisible hit path sharing the identical `d`. The hit path keeps
     `vector-effect: non-scaling-stroke`, so its 14px grab band stays 14 screen
     pixels at any zoom. Both paths sit in `#canvas-links`, which stays
     `pointer-events: none`: only the hit path re-enables pointer events, and
     `#canvas-nodes` is a later sibling so nodes, ports and the toolbar are
     never covered. Port drags and the temporary link get no hit path. */
  function renderLinksNow() {
    const lines = [];
    const selected = key => state.selectedLinks.has(key) ? ' selected' : '';
    generationNodes().forEach(node => (node.orderedInputIds || []).forEach((ref, index) => {
      const parsed = core.parseResultRefId(ref);
      const sourceElement = parsed
        ? $(`[data-node-id="${parsed.nodeId}"] [data-result-id="${parsed.resultId}"] .tile-port`)
        : $(`[data-node-id="${ref}"] .output-port`);
      const target = $(`[data-node-id="${node.id}"] .input-port`);
      const viewport = $('#canvas-viewport');
      if (!sourceElement || !target || !viewport) return;
      const rect = viewport.getBoundingClientRect();
      const a = core.rectCenterToWorld(sourceElement.getBoundingClientRect(), rect, state.viewport);
      const b = core.rectCenterToWorld(target.getBoundingClientRect(), rect, state.viewport);
      const path = curvePath(a, b);
      const key = linkKey(ref, node.id);
      const meta = `data-source="${escapeHtml(ref)}" data-target="${node.id}" data-order="${index + 1}"`;
      lines.push(`<path class="input-link${selected(key)}" ${meta} data-start-x="${a.x}" data-start-y="${a.y}" data-end-x="${b.x}" data-end-y="${b.y}" d="${path}"/>`);
      lines.push(`<path class="link-hit${selected(key)}" ${meta} d="${path}"/>`);
    }));
    if (state.connecting) lines.push(`<path class="temporary-link" d="${curvePath(state.connecting.start, state.connecting.current)}"/>`);
    $('#canvas-links').innerHTML = lines.join(''); updateMinimap();
  }
  function scheduleLinks() { cancelAnimationFrame(linkFrame); linkFrame = requestAnimationFrame(renderLinksNow); }
  function applyViewport() { $('#canvas-world').style.transform = `translate(${state.viewport.x}px,${state.viewport.y}px) scale(${state.viewport.zoom})`; $('#zoom-reset').textContent = `${Math.round(state.viewport.zoom * 100)}%`; scheduleLinks(); }
  function observeLayout() { resizeObserver?.disconnect(); resizeObserver = new ResizeObserver(() => scheduleLinks()); $$('.canvas-node').forEach(el => resizeObserver.observe(el)); }
  function render() { cancelAnimationFrame(renderFrame); $('#canvas-nodes').innerHTML = state.nodes.map(node => core.isGenerationNode(node) ? generationMarkup(node) : imageMarkup(node)).join(''); $('#canvas-empty').hidden = state.nodes.length > 0; applyViewport(); renderFrame = requestAnimationFrame(() => { observeLayout(); scheduleLinks(); }); }
  function scheduleLayoutRecompute() { requestAnimationFrame(() => requestAnimationFrame(() => { scheduleLinks(); updateMinimap(); })); }
  /* The header chevron is the only control that returns a card to the collapsed
     result summary, so it also owns the expand pin. */
  function toggleGeneration(node) { if (!core.isGenerationNode(node)) return; node.expanded = !node.expanded; node.expandedPinned = node.expanded; render(); scheduleLayoutRecompute(); scheduleSave(); }

  function nodeWorldRects() { return state.nodes.map(node => { const el = $(`[data-node-id="${node.id}"]`); const rect = el?.getBoundingClientRect(); return {x: node.x, y: node.y, width: node.width, height: rect ? rect.height / state.viewport.zoom : 240, type: node.type}; }); }
  function updateMinimap() {
    const svg = $('#minimap-map'); if (!svg || svg.hidden) return;
    const viewportRect = $('#canvas-viewport').getBoundingClientRect(); const visible = core.visibleWorld(viewportRect, state.viewport); const rects = nodeWorldRects();
    minimapGeometry = core.minimapGeometry(rects, visible);
    svg.innerHTML = rects.map((rect, index) => { const m = minimapGeometry.nodes[index]; return `<rect class="mini-node ${rect.type}" x="${m.x}" y="${m.y}" width="${m.width}" height="${m.height}"/>`; }).join('') + `<rect class="mini-viewport" x="${minimapGeometry.viewport.x}" y="${minimapGeometry.viewport.y}" width="${minimapGeometry.viewport.width}" height="${minimapGeometry.viewport.height}"/>`;
    svg.dataset.nodeCount = String(rects.length);
  }
  function recenterFromMinimap(event) { if (!minimapGeometry) return; const rect = $('#minimap-map').getBoundingClientRect(); const map = {x: (event.clientX - rect.left) * 176 / rect.width, y: (event.clientY - rect.top) * 112 / rect.height}; const world = core.minimapPointToWorld(map, minimapGeometry); const viewport = $('#canvas-viewport').getBoundingClientRect(); state.viewport.x = viewport.width / 2 - world.x * state.viewport.zoom; state.viewport.y = viewport.height / 2 - world.y * state.viewport.zoom; applyViewport(); }

  function imageSize(src) { return new Promise((resolve, reject) => { const image = new Image(); image.onload = () => resolve({w: image.naturalWidth, h: image.naturalHeight}); image.onerror = () => reject(new Error('无法读取图片，请确认文件未损坏')); image.src = src; }); }
  function transferFiles(source) {
    const files = [...(source?.files || [])];
    const itemFiles = [...(source?.items || [])].filter(item => item.kind === 'file').map(item => item.getAsFile?.()).filter(Boolean);
    return [...new Set([...files, ...itemFiles])];
  }
  function pasteEditableTarget(target) {
    const active = document.activeElement;
    return Boolean(target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])') || active?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),dialog form'));
  }
  function hasUrlOnlyTransfer(source) {
    if (!source) return false;
    const uri = source.getData?.('text/uri-list') || '';
    const text = source.getData?.('text/plain') || '';
    const html = source.getData?.('text/html') || '';
    return Boolean(uri.trim() || /^https?:\/\/\S+$/i.test(text.trim()) || /<img\b[^>]*\bsrc\s*=/i.test(html));
  }
  async function importExternalImages(source, point, options = {}) {
    const candidates = Array.isArray(source) ? source : transferFiles(source);
    if (!candidates.length) {
      if (options.warnUrl && hasUrlOnlyTransfer(source)) toast(urlOnlyMessage, true);
      return 0;
    }
    const valid = [];
    let unsupported = false; let oversized = false;
    candidates.forEach((candidate, index) => {
      const file = candidate instanceof File ? candidate : new File([candidate], `clipboard-image-${index + 1}`, {type: candidate.type});
      if (!acceptedImageTypes.has(file.type.toLowerCase())) unsupported = true;
      else if (file.size > maxLocalImageBytes) oversized = true;
      else valid.push(file);
    });
    let imported = 0;
    for (const file of valid) {
      const id = uid('image'); const src = URL.createObjectURL(file);
      try {
        const size = await imageSize(src); const index = imported;
        state.nodes.push({id, type: core.IMAGE, x: point.x + index * imageOffset, y: point.y + index * imageOffset, width: 280, aspect: `${size.w}/${size.h}`, naturalWidth: size.w, naturalHeight: size.h, src, name: file.name || '粘贴的图片', localOnly: true, needsReselect: false});
        localFiles.set(id, file); objectUrls.set(id, src); imported += 1;
      } catch (error) { URL.revokeObjectURL(src); toast(error.message, true); }
    }
    if (imported) { render(); scheduleSave(); }
    if (oversized) toast('单张图片不能超过 30 MB', true);
    else if (unsupported) toast('仅支持 PNG、JPEG 和 WebP 图片', true);
    return imported;
  }
  function reselectImage(node) { const input = document.createElement('input'); input.type = 'file'; input.accept = 'image/png,image/jpeg,image/webp'; input.addEventListener('change', async () => { const file = input.files?.[0]; if (!file) return; if (!acceptedImageTypes.has(file.type.toLowerCase())) { toast('仅支持 PNG、JPEG 和 WebP 图片', true); return; } if (file.size > maxLocalImageBytes) { toast('单张图片不能超过 30 MB', true); return; } const src = URL.createObjectURL(file); try { const size = await imageSize(src); if (objectUrls.has(node.id)) URL.revokeObjectURL(objectUrls.get(node.id)); localFiles.set(node.id, file); objectUrls.set(node.id, src); Object.assign(node, {src, name: file.name, aspect: `${size.w}/${size.h}`, naturalWidth: size.w, naturalHeight: size.h, localOnly: true, needsReselect: false}); render(); scheduleSave(); } catch (error) { URL.revokeObjectURL(src); toast(error.message, true); } }, {once: true}); input.click(); }

  function syncDraggingClass() { $('#canvas-viewport')?.classList.toggle('dragging', Boolean(drag || pan || state.connecting)); }
  function cancelConnection() { if (!state.connecting) return false; state.connecting = null; $('.canvas-shell')?.classList.remove('connecting'); $$('.generation-node.drop-target').forEach(node => node.classList.remove('drop-target')); syncDraggingClass(); scheduleLinks(); return true; }
  function beginConnection(event, node, ref, portSelector) {
    event.preventDefault(); event.stopPropagation();
    const start = portWorld(node.id, portSelector); if (!start) return;
    state.connecting = {sourceId: ref, sourceNodeId: node.id, pointerId: event.pointerId, start, current: start};
    $('.canvas-shell')?.classList.add('connecting'); syncDraggingClass(); event.currentTarget.setPointerCapture?.(event.pointerId); scheduleLinks();
  }
  function finishConnection(event) {
    if (!state.connecting || event.pointerId !== state.connecting.pointerId) return;
    const connection = state.connecting; state.connecting = null; $('.canvas-shell')?.classList.remove('connecting'); syncDraggingClass();
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest('.generation-node');
    if (target) { addInput(target.dataset.nodeId, connection.sourceId); return; }
    const invalid = document.elementFromPoint(event.clientX, event.clientY)?.closest('.canvas-node,.canvas-toolbar,.workspace-bar');
    const viewport = document.elementFromPoint(event.clientX, event.clientY)?.closest('#canvas-viewport');
    if (!viewport || invalid) { scheduleLinks(); return; }
    const source = core.resolveInputRef(state.nodes, connection.sourceId);
    if (!source) { scheduleLinks(); return; }
    const point = worldPoint(event.clientX, event.clientY);
    const parameters = source.kind === 'result' ? source.result.parameters || {} : {};
    addGeneration(point.x, point.y, {
      orderedInputIds: [connection.sourceId],
      parentGenerationId: source.generationId || '',
      prompt: source.kind === 'result' ? source.node.prompt || '' : '',
      profileId: source.kind === 'result' ? source.node.profileId || '' : '',
      ratio: parameters.ratio,
      resolution: parameters.resolution,
      quality: parameters.quality,
    });
  }
  /* Pointer capture is taken on the first real movement, never on pointerdown:
     capturing the viewport immediately retargets the following click/dblclick
     to the viewport, which silently killed result-image single click (expand)
     and double click (viewer). */
  function startNodeDrag(event, node) {
    if (event.button !== 0 || event.target.closest('button,input,textarea,select,a,summary,[draggable="true"]')) return;
    event.preventDefault(); event.stopPropagation();
    pointerSelectionId = node.id;
    if (!state.selectedIds.has(node.id)) selectNode(node.id, event.shiftKey, false);
    const origins = [...state.selectedIds].map(id => { const item = nodeById(id); return item && {id, x: item.x, y: item.y}; }).filter(Boolean);
    drag = {pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, origins, moved: false, captured: false}; origins.forEach(item => $(`[data-node-id="${item.id}"]`)?.classList.add('dragging')); syncDraggingClass();
  }
  function movePointer(event) {
    if (state.connecting && event.pointerId === state.connecting.pointerId) { state.connecting.current = worldPoint(event.clientX, event.clientY); $$('.generation-node.drop-target').forEach(node => node.classList.remove('drop-target')); document.elementFromPoint(event.clientX, event.clientY)?.closest('.generation-node')?.classList.add('drop-target'); scheduleLinks(); return; }
    if (drag && event.pointerId === drag.pointerId) {
      const dx = (event.clientX - drag.startX) / state.viewport.zoom; const dy = (event.clientY - drag.startY) / state.viewport.zoom;
      if (Math.hypot(dx, dy) > 2) {
        if (!drag.moved) { drag.moved = true; if (!drag.captured) { drag.captured = true; event.currentTarget.setPointerCapture?.(event.pointerId); } }
        drag.origins.forEach(origin => { const node = nodeById(origin.id); if (!node) return; node.x = origin.x + dx; node.y = origin.y + dy; const el = $(`[data-node-id="${node.id}"]`); if (el) { el.style.left = `${node.x}px`; el.style.top = `${node.y}px`; } }); scheduleLinks();
      }
      return;
    }
    if (pan && event.pointerId === pan.pointerId) { const dx = event.clientX - pan.startX; const dy = event.clientY - pan.startY; if (Math.hypot(dx, dy) > 4) pan.moved = true; state.viewport.x = pan.x + dx; state.viewport.y = pan.y + dy; applyViewport(); }
  }
  function clearPointerInteraction(pointerId) {
    let changed = false;
    if (drag && (pointerId == null || drag.pointerId === pointerId)) { drag.origins.forEach(item => $(`[data-node-id="${item.id}"]`)?.classList.remove('dragging')); drag = null; changed = true; }
    if (pan && (pointerId == null || pan.pointerId === pointerId)) { pan = null; changed = true; }
    syncDraggingClass();
    return changed;
  }
  function endPointer(event) {
    if (state.connecting) finishConnection(event);
    if (drag && event.pointerId === drag.pointerId) { suppressClick = drag.moved; clearPointerInteraction(event.pointerId); scheduleSave(); }
    if (pan && event.pointerId === pan.pointerId) { if (!pan.moved) clearSelection(); suppressClick = pan.moved; clearPointerInteraction(event.pointerId); scheduleSave(); }
    syncDraggingClass();
  }
  function reorderInput(nodeId, ref, beforeRef) { const node = nodeById(nodeId); if (!core.isGenerationNode(node)) return; const list = node.orderedInputIds.filter(item => item !== ref); const target = list.indexOf(beforeRef); list.splice(target < 0 ? list.length : target, 0, ref); node.orderedInputIds = list; syncDirty(node); render(); scheduleSave(); }

  async function sourceFile(ref) {
    const source = core.resolveInputRef(state.nodes, ref);
    if (!source) throw new Error('参考图不可用，请重新选择');
    if (source.kind === 'image') {
      if (localFiles.has(ref)) return localFiles.get(ref);
      if (source.localOnly) throw new Error('本地参考图需要重新选择后才能生成');
      throw new Error('参考图不可用，请重新选择');
    }
    const response = await fetch(source.src);
    if (!response.ok) throw new Error('无法读取历史结果原图');
    const blob = await response.blob();
    const extension = blob.type.includes('png') ? 'png' : blob.type.includes('webp') ? 'webp' : 'jpg';
    return new File([blob], `generation-${source.generationId || source.result.id}.${extension}`, {type: blob.type});
  }
  async function submitOne(request, result, parentGenerationId) {
    const data = new FormData();
    data.append('prompt', request.prompt.trim());
    data.append('profile_id', request.profileId);
    data.append('ratio', request.ratio);
    data.append('resolution', request.resolution);
    data.append('quality', request.quality || 'standard');
    data.append('parent_generation_id', parentGenerationId || '');
    data.append('idempotency_key', uid('generation').replace(/-/g, '').slice(0, 64));
    for (const ref of request.orderedInputIds || []) data.append('references', await sourceFile(ref));
    const payload = await responseJson(await fetch(`/api/projects/${projectId}/generations`, {method: 'POST', headers: csrfHeaders, body: data}));
    result.generationId = payload.id;
    result.status = payload.status || 'queued';
    scheduleSave();
    return payload;
  }
  function editorFocused(node) {
    return Boolean(document.activeElement?.closest?.(`[data-node-id="${node.id}"] .generation-editor`));
  }
  /* One immutable Generation per result; the node keeps its previous batch
     visible until the new attempt settles, then swaps its active batch. */
  function settleAttempt(node) {
    const attempt = node.attempt;
    if (!attempt || attempt.settled) return false;
    const results = core.batchResults(attempt);
    if (!results.length || results.some(result => ['queued', 'running'].includes(result.status))) return false;
    attempt.settled = true;
    const anySucceeded = results.some(result => result.status === 'succeeded');
    const previous = core.batchResults(node.activeBatch);
    if (anySucceeded || !previous.length) {
      node.activeBatch = {id: attempt.id, createdAt: attempt.createdAt, parentGenerationId: attempt.parentGenerationId, request: attempt.request, results};
      node.attempt = null;
      node.error = anySucceeded ? '' : (results.map(result => result.error).find(Boolean) || '生成失败，可安全重试');
      node.primaryResultId = anySucceeded ? (results.find(result => result.status === 'succeeded')?.id || '') : '';
      node.dirty = core.isDirty(node);
      /* Only a freshly submitted generation may return the card to its result
         summary. A card the user expanded by hand keeps its editor open until
         the header toggle closes it. */
      if (!anySucceeded) { node.expanded = true; node.expandedPinned = true; }
      else if (!node.expandedPinned && !editorFocused(node)) node.expanded = false;
      return true;
    }
    attempt.promoted = false;
    node.error = results.map(result => result.error).find(Boolean) || '生成失败，可安全重试';
    node.dirty = core.isDirty(node);
    return true;
  }
  /* A card that produced several images is split into sibling cards once the
     batch settles, so every image can be opened, regenerated and downloaded on
     its own. The submitting card keeps result #1 and stays where it is. */
  function visibleWorldRect() {
    const viewport = $('#canvas-viewport');
    return viewport ? core.visibleWorld(viewport.getBoundingClientRect(), state.viewport) : null;
  }
  function cardRowHeight(node) {
    const element = $(`[data-node-id="${node.id}"]`);
    const height = element ? element.getBoundingClientRect().height / state.viewport.zoom : 0;
    return height > 120 ? Math.min(1200, Math.round(height)) : undefined;
  }
  function splitOptions() { return {uid, visible: visibleWorldRect(), rowHeight: cardRowHeight}; }
  function splitSettledResults() {
    const outcome = core.splitGenerationNodes(state.nodes, splitOptions());
    if (!outcome.changed) return false;
    state.nodes = outcome.nodes;
    render(); scheduleLayoutRecompute(); scheduleSave();
    toast(`多图已拆分为 ${outcome.created.length + outcome.splits} 张独立卡片，每张可单独下载 PNG`);
    return true;
  }
  async function generate(node) {
    if (!core.isGenerationNode(node) || node.submitting) return;
    if (node.attempt && !node.attempt.settled) { toast('该节点正在生成，请等待当前任务结束', true); return; }
    if (!node.prompt.trim()) { toast('请先输入提示词', true); $(`[data-node-id="${node.id}"] textarea`)?.focus(); return; }
    const profile = profileById(node.profileId); if (!profile?.enabled) { toast('当前模型不可用，请选择已启用模型', true); return; }
    const missing = core.firstMissingLocalInput(state.nodes, node, id => localFiles.has(id)); if (missing) { selectNode(missing.id); toast('本地参考图需要重新选择后才能生成', true); return; }
    const request = core.requestSnapshot(node);
    const count = core.clampCount(request.count);
    const parent = lineageParentId(node);
    const aspect = core.ratioToAspect(request.ratio);
    const results = Array.from({length: count}, () => core.normalizeResult({id: uid('result'), status: 'queued', aspect, provider: profile.provider, modelLabel: profile.label, prompt: node.prompt, profileId: profile.id, parameters: {ratio: request.ratio, resolution: request.resolution, quality: request.quality, count}}));
    node.attempt = {id: uid('batch'), createdAt: new Date().toISOString(), parentGenerationId: parent, request, results, settled: false, promoted: false};
    /* Submitting is an explicit "show me the new result" intent, so the card is
       allowed to fall back to its result summary once this batch settles. */
    node.expandedPinned = false;
    node.submitting = true; node.error = '';
    render(); scheduleSave();
    await Promise.all(results.map(async result => {
      try { await submitOne(request, result, parent); }
      catch (error) { result.status = 'failed'; result.error = error.message; }
    }));
    node.submitting = false;
    settleAttempt(node);
    splitSettledResults();
    render(); scheduleSave(); startPolling();
  }
  function updateResults(items) {
    let changed = false;
    const byId = new Map(items.map(item => [item.id, item]));
    generationNodes().forEach(node => {
      [...core.batchResults(node.activeBatch), ...core.batchResults(node.attempt)].forEach(result => {
        if (!result.generationId) return;
        const item = byId.get(result.generationId); if (!item) return;
        const before = `${result.status}|${result.artifactUrl}|${result.sentiment}`;
        Object.assign(result, {status: item.status, artifactUrl: item.artifact_url, error: item.error, canRetry: item.can_retry, sentiment: item.sentiment || '', provider: item.provider, modelLabel: item.model_label, prompt: item.prompt, parameters: item.parameters, profileId: item.profile_id || result.profileId, createdAt: item.created_at || result.createdAt});
        if (before !== `${result.status}|${result.artifactUrl}|${result.sentiment}`) { changed = true; emitResult(node, result); }
      });
      if (settleAttempt(node)) changed = true;
    });
    if (splitSettledResults()) changed = true;
    if (changed) { render(); scheduleSave(); }
    return generationNodes().some(node => [...core.batchResults(node.activeBatch), ...core.batchResults(node.attempt)].some(result => ['queued', 'running'].includes(result.status)));
  }
  async function poll() { clearTimeout(pollTimer); try { const data = await responseJson(await fetch(`/api/projects/${projectId}/generations?limit=100`)); if (updateResults(data.items)) pollTimer = setTimeout(poll, 2500); } catch (error) { console.warn('任务状态刷新失败', error); pollTimer = setTimeout(poll, 5000); } }
  function startPolling() { poll(); }
  async function setSentiment(node, value, button) {
    const primary = primaryOf(node);
    if (!primary?.generationId) return;
    button.disabled = true;
    try { await responseJson(await fetch(`/api/projects/${projectId}/generations/${primary.generationId}/sentiment`, {method: 'POST', headers: {'Content-Type': 'application/json', ...csrfHeaders}, body: JSON.stringify({sentiment: value})})); primary.sentiment = value; render(); scheduleSave(); emitResult(node, primary, 'sentiment'); toast(`已标记为${sentimentNames[value]}`); } catch (error) { toast(error.message, true); button.disabled = false; }
  }
  function findResultByGeneration(generationId) { return core.findResult(state.nodes, generationId); }
  async function retryResult(generationId, button) {
    const found = findResultByGeneration(generationId);
    if (!found) return;
    if (button) button.disabled = true;
    try { const data = await responseJson(await fetch(`/api/projects/${projectId}/generations/${generationId}/retry`, {method: 'POST', headers: csrfHeaders})); Object.assign(found.result, {status: data.status, error: '', canRetry: false}); found.node.error = ''; render(); scheduleSave(); startPolling(); } catch (error) { toast(error.message, true); if (button) button.disabled = false; }
  }

  function importCanvas(payload) {
    state.viewport = {x: Number(payload.viewport?.x) || 0, y: Number(payload.viewport?.y) || 0, zoom: Number(payload.viewport?.zoom) || 1};
    state.selectedIds.clear(); state.selectedLinks.clear();
    const known = [core.IMAGE, core.GENERATION, core.LEGACY_REQUEST, core.LEGACY_RESULT];
    const loaded = (Array.isArray(payload.nodes) ? payload.nodes : []).filter(node => node && known.includes(node.type));
    const migrated = core.migrateCanvasNodes(loaded, splitOptions());
    state.nodes = core.restoreCanvasNodes(migrated.nodes);
    if (!state.nodes.length && payload.draft) { const draft = payload.draft; state.nodes.push(defaultGeneration(220, 160, {profileId: draft.profile, ratio: draft.ratio, resolution: draft.resolution, prompt: draft.prompt})); }
    return migrated.migrated;
  }
  function focusAndCenterNode(node) { const viewport = $('#canvas-viewport'); const rect = viewport.getBoundingClientRect(); const height = $(`[data-node-id="${node.id}"]`)?.getBoundingClientRect().height / state.viewport.zoom || 300; clearLinkSelection(); state.selectedIds = new Set([node.id]); state.viewport.x = rect.width / 2 - (node.x + node.width / 2) * state.viewport.zoom; state.viewport.y = rect.height / 2 - (node.y + height / 2) * state.viewport.zoom; render(); scheduleSave(); }
  function pulseResult(nodeId, resultId) {
    requestAnimationFrame(() => {
      const tile = $(`[data-node-id="${nodeId}"] [data-result-id="${resultId}"]`);
      if (!tile) return;
      tile.classList.add('attention');
      window.setTimeout(() => tile.classList.remove('attention'), 1600);
    });
  }
  async function applyHistoryQuery() {
    const query = new URLSearchParams(location.search); const action = query.get('action'); const generationId = query.get('generation');
    if (!generationId || !['locate', 'continue'].includes(action)) return;
    try {
      const item = await responseJson(await fetch(`/api/projects/${projectId}/generations/${generationId}`));
      const outcome = core.historyAction(state.nodes, item, action, uid);
      if (action === 'continue') addGeneration(outcome.create.x, outcome.create.y, outcome.create);
      else { focusAndCenterNode(outcome.node); pulseResult(outcome.node.id, outcome.result.id); }
      if (outcome.createdNode) scheduleSave();
    } catch (error) { toast(error.message, true); }
  }
  function fitView() { if (!state.nodes.length) { state.viewport = {x: 0, y: 0, zoom: 1}; applyViewport(); return; } const rects = nodeWorldRects(); const minX = Math.min(...rects.map(n => n.x)); const minY = Math.min(...rects.map(n => n.y)); const maxX = Math.max(...rects.map(n => n.x + n.width)); const maxY = Math.max(...rects.map(n => n.y + n.height)); const viewport = $('#canvas-viewport').getBoundingClientRect(); const zoom = Math.max(.35, Math.min(1, (viewport.width - 100) / Math.max(1, maxX - minX), (viewport.height - 100) / Math.max(1, maxY - minY))); state.viewport = {x: 50 - minX * zoom, y: 50 - minY * zoom, zoom}; applyViewport(); scheduleSave(); }
  function zoomAt(next, cx, cy) { closeContextMenu(); const rect = $('#canvas-viewport').getBoundingClientRect(); const old = state.viewport.zoom; const zoom = Math.max(.35, Math.min(1.8, next)); const px = cx - rect.left; const py = cy - rect.top; const wx = (px - state.viewport.x) / old; const wy = (py - state.viewport.y) / old; state.viewport.x = px - wx * zoom; state.viewport.y = py - wy * zoom; state.viewport.zoom = zoom; applyViewport(); scheduleSave(); }

  function viewerBounds() { const stage = $('.viewer-stage', $('#image-viewer')); return stage?.getBoundingClientRect(); }
  function clampViewerPan() {
    const image = $('img', $('#image-viewer')); const bounds = viewerBounds(); if (!bounds || !image.naturalWidth) return;
    const maxX = Math.max(0, (image.naturalWidth * viewerState.scale - bounds.width) / 2);
    const maxY = Math.max(0, (image.naturalHeight * viewerState.scale - bounds.height) / 2);
    viewerState.panX = Math.max(-maxX, Math.min(maxX, viewerState.panX)); viewerState.panY = Math.max(-maxY, Math.min(maxY, viewerState.panY));
  }
  function applyViewerTransform() {
    const dialog = $('#image-viewer'); const image = $('img', dialog); if (!image.naturalWidth) return;
    clampViewerPan(); image.style.width = `${image.naturalWidth}px`; image.style.height = `${image.naturalHeight}px`; image.style.transform = `translate(-50%, -50%) translate(${viewerState.panX}px, ${viewerState.panY}px) scale(${viewerState.scale})`;
    $('[data-view-zoom]', dialog).textContent = `${Math.round(viewerState.scale * 100)}%`; const pannable = image.naturalWidth * viewerState.scale > viewerBounds().width + 1 || image.naturalHeight * viewerState.scale > viewerBounds().height + 1; $('.viewer-stage', dialog).classList.toggle('can-pan', pannable);
  }
  function fitViewer() { const image = $('img', $('#image-viewer')); const bounds = viewerBounds(); if (!image.naturalWidth || !bounds) return; viewerState.scale = Math.max(.1, Math.min(8, bounds.width / image.naturalWidth, bounds.height / image.naturalHeight)); viewerState.panX = 0; viewerState.panY = 0; viewerState.fit = true; applyViewerTransform(); }
  function actualViewer() { viewerState.scale = 1; viewerState.panX = 0; viewerState.panY = 0; viewerState.fit = false; applyViewerTransform(); }
  function zoomViewer(next, clientX, clientY) {
    const bounds = viewerBounds(); if (!bounds) return; const old = viewerState.scale; const scale = Math.max(.1, Math.min(8, next)); const x = (clientX ?? bounds.left + bounds.width / 2) - bounds.left - bounds.width / 2; const y = (clientY ?? bounds.top + bounds.height / 2) - bounds.top - bounds.height / 2;
    viewerState.panX = x - (x - viewerState.panX) * scale / old; viewerState.panY = y - (y - viewerState.panY) * scale / old; viewerState.scale = scale; viewerState.fit = false; applyViewerTransform();
  }
  function openViewer({url, title, downloadable = true, invoker = null} = {}) {
    if (!url) return;
    const dialog = $('#image-viewer'); const image = $('img', dialog);
    viewerState.invoker = invoker || document.activeElement?.closest?.('[data-open-image],[data-open-result],.result-tile,.image-node') || document.activeElement;
    Object.assign(viewerState, {scale: 1, panX: 0, panY: 0, fit: true, pointerId: null});
    image.alt = title || '结果完整预览'; $('#viewer-title').textContent = title || '查看大图';
    $('[data-view-original]', dialog).href = url;
    const download = $('[data-view-download]', dialog); download.hidden = !downloadable; download.href = downloadable ? downloadUrl(url) : ''; download.dataset.resultUrl = downloadable ? url : '';
    image.onload = fitViewer; image.src = url; dialog.showModal();
    if (image.complete && image.naturalWidth) requestAnimationFrame(fitViewer);
    requestAnimationFrame(() => $('[data-view-zoom-in]', dialog).focus());
  }
  function visibleWorldCenter() { const rect = $('#canvas-viewport').getBoundingClientRect(); return {x: (rect.width / 2 - state.viewport.x) / state.viewport.zoom, y: (rect.height / 2 - state.viewport.y) / state.viewport.zoom}; }
  function restoreWorldCenter(center) { const rect = $('#canvas-viewport').getBoundingClientRect(); state.viewport.x = rect.width / 2 - center.x * state.viewport.zoom; state.viewport.y = rect.height / 2 - center.y * state.viewport.zoom; applyViewport(); scheduleLinks(); scheduleSave(); }
  function setSidebarCollapsed(collapsed) { const center = visibleWorldCenter(); $('#workspace-layout').classList.toggle('sidebar-collapsed', collapsed); $('#sidebar-collapse').setAttribute('aria-expanded', String(!collapsed)); $('#sidebar-collapse').setAttribute('aria-label', collapsed ? '展开项目侧栏' : '收起项目侧栏'); $('#sidebar-collapse').title = collapsed ? '展开项目侧栏' : '收起项目侧栏'; localStorage.setItem('image-hub-sidebar-collapsed', String(collapsed)); requestAnimationFrame(() => restoreWorldCenter(center)); }
  let drawerReturnFocus = null;
  function setProjectDrawer(open) { const layout = $('#workspace-layout'); const button = $('#mobile-projects-button'); if (!matchMedia('(max-width: 768px)').matches) open = false; if (open) drawerReturnFocus = document.activeElement; layout.classList.toggle('drawer-open', open); document.body.classList.toggle('project-drawer-open', open); $('#project-scrim').hidden = !open; button.setAttribute('aria-expanded', String(open)); if (open) requestAnimationFrame(() => $('.project-list-item.current', $('#project-sidebar'))?.focus()); else drawerReturnFocus?.focus?.({preventScroll: true}); scheduleLinks(); }
  function createProject() { setProjectDrawer(false); $('#quick-create-project').requestSubmit(); }
  function openRenameProjectDialog(button) {
    setProjectDrawer(false);
    const dialog = $('#project-rename-dialog');
    const form = $('form', dialog);
    const input = $('#project-rename-name', dialog);
    form.action = `/projects/${encodeURIComponent(button.dataset.renameProjectId)}/rename`;
    input.value = button.dataset.renameProjectName || '';
    dialog.showModal();
    requestAnimationFrame(() => { input.focus(); input.select(); });
  }
  function closeRenameProjectDialog() { const dialog = $('#project-rename-dialog'); if (dialog.open) dialog.close(); }
  function clearRenameProjectDialog() { const dialog = $('#project-rename-dialog'); const form = $('form', dialog); const input = $('#project-rename-name', dialog); form.action = ''; input.value = ''; input.setCustomValidity(''); }
  function closeViewer() { const dialog = $('#image-viewer'); if (!dialog.open) return false; dialog.close(); const image = $('img', dialog); image.onload = null; image.removeAttribute('src'); image.removeAttribute('style'); $('.viewer-stage', dialog).classList.remove('can-pan', 'is-panning'); Object.assign(viewerState, {scale: 1, panX: 0, panY: 0, fit: true, pointerId: null}); const target = viewerState.invoker; viewerState.invoker = null; requestAnimationFrame(() => target?.isConnected && target.focus?.({preventScroll: true})); return true; }
  function closeShortcut() { $('#shortcut-popover').hidden = true; }
  function openShortcut() { closeContextMenu(); const pop = $('#shortcut-popover'); pop.hidden = false; $('[data-close-overlay]', pop).focus(); }
  function closeContextMenu(restore = false) { const menu = $('#context-menu'); if (menu.hidden) return false; menu.hidden = true; if (restore && context?.focusId) $(`[data-node-id="${context.focusId}"]`)?.focus(); context = null; return true; }
  function contextItems(node, link = null) {
    if (link) return [{label: '移除连线', action: 'unlink'}];
    if (!node) return [{label: '上传图片', action: 'upload'}, {label: '新建生图', action: 'request'}, {label: '粘贴', action: 'paste', disabled: !canvasClipboard}, {label: '适应内容', action: 'fit'}, {label: '快捷键', action: 'shortcuts'}];
    const common = [{label: '复制', action: 'copy'}, {label: '从画布移除', action: 'remove'}];
    if (core.isGenerationNode(node)) return [...common, {label: node.expanded ? '收起为结果简洁态' : '展开编辑参数', action: 'toggle'}, {label: '从主图创建生图', action: 'derive', disabled: !primaryOf(node)?.artifactUrl}];
    return [...common, {label: '打开大图', action: 'open', disabled: !node.src}, {label: '从此图创建生图', action: 'derive'}];
  }
  /* Right-clicking a connection selects it first, so the menu and the keyboard
     share one target. Right-clicking blank canvas or a node keeps the existing
     items untouched. */
  function openContextMenu(event, node, link = null) {
    event.preventDefault(); closeShortcut(); activePickerId = '';
    if (link && !state.selectedLinks.has(linkKey(link.source, link.target))) selectLink(link.source, link.target, false);
    const menu = $('#context-menu'); const point = worldPoint(event.clientX, event.clientY); context = {nodeId: node?.id || '', point, focusId: node?.id || ''}; menu.innerHTML = contextItems(node, link).map(item => `<button type="button" role="menuitem" data-context-action="${item.action}" ${item.disabled ? 'disabled' : ''}>${item.label}</button>`).join(''); menu.hidden = false; const margin = 8; const rect = menu.getBoundingClientRect(); menu.style.left = `${Math.max(margin, Math.min(event.clientX, innerWidth - rect.width - margin))}px`; menu.style.top = `${Math.max(margin, Math.min(event.clientY, innerHeight - rect.height - margin))}px`; menu.focus(); $('button:not(:disabled)', menu)?.focus(); }
  /* Every download entry (result rail, viewer) funnels through the single
     `?download=true` URL, which the server answers with a transcoded PNG. The
     blob route is used instead of a bare anchor so a readable message replaces
     a raw JSON error page when the server refuses the conversion. */
  async function downloadResult(url) {
    if (!url) return;
    try {
      const response = await fetch(downloadUrl(url), {credentials: 'same-origin'});
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || '下载失败，请稍后重试');
      }
      const blob = await response.blob();
      if (blob.type && !blob.type.includes('png')) throw new Error('服务端未返回 PNG，请稍后重试');
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = downloadFileName(response.headers.get('Content-Disposition'), url);
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10000);
    } catch (error) { toast(error.message || '下载失败，请稍后重试', true); }
  }
  function runContextAction(action) {
    const node = nodeById(context?.nodeId); const point = context?.point;
    if (action === 'unlink') removeLinks([...state.selectedLinks]);
    else if (action === 'upload') { pendingUploadPoint = point; $('#canvas-upload').click(); }
    else if (action === 'request') addGeneration(point.x, point.y);
    else if (action === 'paste') pasteClipboard(point);
    else if (action === 'fit') fitView();
    else if (action === 'shortcuts') openShortcut();
    else if (node) {
      if (action === 'copy') { if (!state.selectedIds.has(node.id)) selectNode(node.id, false, false); copySelection(); }
      else if (action === 'remove') removeIds(state.selectedIds.has(node.id) ? state.selectedIds : [node.id]);
      else if (action === 'toggle') toggleGeneration(node);
      else if (action === 'open') openViewer({url: node.src, title: node.name, downloadable: false, invoker: $(`[data-node-id="${node.id}"]`)});
      else if (action === 'derive') {
        const primary = primaryOf(node);
        const ref = primary ? core.resultRefId(node.id, primary.id) : node.id;
        addGeneration(node.x + node.width + 120, node.y, {orderedInputIds: [ref], parentGenerationId: primary?.generationId || '', prompt: node.prompt, profileId: node.profileId, ratio: primary?.parameters?.ratio || node.ratio, resolution: primary?.parameters?.resolution || node.resolution, quality: primary?.parameters?.quality || node.quality});
      }
    }
    closeContextMenu();
  }

  function setPickerOpen(node, kind, open) {
    activePickerId = open ? node.id : '';
    activePickerKind = open ? kind : '';
    render();
    if (open) requestAnimationFrame(() => $(`[data-node-id="${node.id}"] .selector-popover:not([hidden]) button:not(:disabled)`)?.focus());
  }
  function closeActivePicker(restore = false) {
    if (!activePickerId) return false;
    const node = nodeById(activePickerId); const kind = activePickerKind;
    activePickerId = ''; activePickerKind = ''; render();
    if (restore) requestAnimationFrame(() => $(`[data-node-id="${node?.id}"] ${kind === 'model' ? '[data-model-trigger]' : '[data-image-settings-trigger]'}`)?.focus({preventScroll: true}));
    return true;
  }
  function selectProfile(node, profileId) {
    const old = comboValue(node.ratio, node.resolution); const profile = profileById(profileId); if (!profile?.enabled) return;
    node.profileId = profile.id; node.provider = profile.provider; const valid = combos(profile); const chosen = valid.find(item => item.value === old) || valid[0];
    node.ratio = chosen?.ratio || '1:1'; node.resolution = chosen?.resolution || '2K'; node.quality = profile.qualities?.includes(node.quality) ? node.quality : (profile.qualities?.[0] || 'standard');
    pickerProviders.set(node.id, profile.provider); const mapped = valid.some(item => item.value === old); syncDirty(node); closeActivePicker(); if (!mapped) toast('已切换为该模型支持的默认尺寸'); scheduleSave(); requestAnimationFrame(() => $(`[data-node-id="${node.id}"] [data-model-trigger]`)?.focus({preventScroll: true}));
  }
  function selectImageSetting(node, field, value) {
    const profile = profileById(node.profileId);
    node[field] = value;
    /* Some models expose a narrower resolution set per ratio (gpt-image-2.5 has
       no 4K for 1:3 / 3:1). Switching to such a ratio would otherwise leave the
       node pinned to a tier the upstream does not publish, so coerce it onto the
       first tier the new ratio actually supports. */
    if (field === 'ratio') {
      const tiers = profileTiers(profile, node.ratio);
      if (tiers.length && !tiers.includes(node.resolution)) node.resolution = tiers[0];
    }
    syncDirty(node); closeActivePicker(); scheduleSave(); requestAnimationFrame(() => $(`[data-node-id="${node.id}"] [data-image-settings-trigger]`)?.focus({preventScroll: true}));
  }

  /* Single click expands in place (and marks the primary result when the batch
     holds several images); double click is handled separately by the viewer.
     Expanding here is a user action, so it pins the card against the automatic
     collapse that a background result refresh may attempt. */
  function activateResult(node, batch, result) {
    const results = core.batchResults(batch);
    let changed = false;
    if (results.length > 1 && results.some(item => item.id === result.id) && node.primaryResultId !== result.id) { node.primaryResultId = result.id; changed = true; }
    if (!node.expanded) { node.expanded = true; changed = true; }
    if (!node.expandedPinned) { node.expandedPinned = true; changed = true; }
    if (changed) { render(); scheduleLayoutRecompute(); }
    scheduleSave();
  }
  function resultInNode(node, resultId) {
    const batch = displayBatch(node);
    if (!batch) return null;
    return core.batchResults(batch).find(result => result.id === resultId) || null;
  }

  document.addEventListener('DOMContentLoaded', async () => {
    try { const payload = await responseJson(await fetch(`/api/projects/${projectId}/canvas`)); if (importCanvas(payload.state || {})) scheduleSave(); } catch (error) { toast(error.message, true); }
    render(); await applyHistoryQuery(); startPolling(); document.fonts?.ready.then(scheduleLinks);
    const collapsed = localStorage.getItem('image-hub-sidebar-collapsed') === 'true'; $('#workspace-layout').classList.toggle('sidebar-collapsed', collapsed); $('#sidebar-collapse').setAttribute('aria-expanded', String(!collapsed));
    if (matchMedia('(max-width: 420px)').matches) { $('#minimap-map').hidden = true; $('#minimap-toggle').setAttribute('aria-expanded', 'false'); }
    const viewport = $('#canvas-viewport');
    $('#canvas-upload').addEventListener('change', event => { const rect = viewport.getBoundingClientRect(); const point = pendingUploadPoint || worldPoint(rect.left + 180, rect.top + 150); pendingUploadPoint = null; importExternalImages([...event.target.files], point); event.target.value = ''; });
    $('#add-request').addEventListener('click', () => { const rect = viewport.getBoundingClientRect(); const p = worldPoint(rect.left + rect.width / 2 - 175, rect.top + 140); addGeneration(p.x, p.y); });
    $('[data-empty-action="request"]').addEventListener('click', () => $('#add-request').click());
    $('#fit-view').addEventListener('click', fitView); $('#zoom-in').addEventListener('click', () => zoomAt(state.viewport.zoom + .1, innerWidth / 2, innerHeight / 2)); $('#zoom-out').addEventListener('click', () => zoomAt(state.viewport.zoom - .1, innerWidth / 2, innerHeight / 2)); $('#zoom-reset').addEventListener('click', () => zoomAt(1, innerWidth / 2, innerHeight / 2));
    viewport.addEventListener('dragover', event => { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'; viewport.classList.add('file-over'); }); viewport.addEventListener('dragleave', () => viewport.classList.remove('file-over')); viewport.addEventListener('drop', event => { event.preventDefault(); viewport.classList.remove('file-over'); importExternalImages(event.dataTransfer, worldPoint(event.clientX, event.clientY), {warnUrl: true}); });
    viewport.addEventListener('wheel', event => { event.preventDefault(); zoomAt(state.viewport.zoom * (event.deltaY < 0 ? 1.08 : .92), event.clientX, event.clientY); }, {passive: false});
    viewport.addEventListener('pointerdown', event => {
      closeContextMenu();
      /* A connection click is consumed before any pan or drag can start, so
         selecting a link never moves the canvas or a node. */
      const linkHit = event.target.closest?.('.link-hit');
      if (linkHit) { event.preventDefault(); if (event.button !== 2) selectLink(linkHit.dataset.source, linkHit.dataset.target, event.shiftKey); return; }
      const nodeEl = event.target.closest('.canvas-node');
      const node = nodeEl && nodeById(nodeEl.dataset.nodeId);
      if (!node) { if (event.button === 0 && (event.target === viewport || event.target.closest('.canvas-world'))) { event.preventDefault(); pan = {pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: state.viewport.x, y: state.viewport.y, moved: false}; viewport.setPointerCapture?.(event.pointerId); syncDraggingClass(); } return; }
      const tilePort = event.target.closest('.tile-port');
      if (tilePort) return beginConnection(event, node, tilePort.dataset.tileRef, '.tile-port');
      if (event.target.closest('.output-port')) return beginConnection(event, node, node.id, '.output-port');
      return startNodeDrag(event, node);
    });
    viewport.addEventListener('pointermove', event => { lastCanvasPointer = worldPoint(event.clientX, event.clientY); movePointer(event); }); viewport.addEventListener('pointerup', endPointer); viewport.addEventListener('pointercancel', event => { const wasActive = Boolean((drag && drag.pointerId === event.pointerId) || (pan && pan.pointerId === event.pointerId) || (state.connecting && state.connecting.pointerId === event.pointerId)); if (state.connecting?.pointerId === event.pointerId) cancelConnection(); clearPointerInteraction(event.pointerId); if (wasActive) scheduleSave(); });
    viewport.addEventListener('selectstart', event => { if (drag || pan || state.connecting) event.preventDefault(); });
    viewport.addEventListener('contextmenu', event => {
      const linkHit = event.target.closest?.('.link-hit');
      if (linkHit) { openContextMenu(event, null, {source: linkHit.dataset.source, target: linkHit.dataset.target}); return; }
      const el = event.target.closest('.canvas-node'); openContextMenu(event, el ? nodeById(el.dataset.nodeId) : null);
    });
    $('#canvas-nodes').addEventListener('dblclick', event => {
      clearTimeout(resultClickTimer); resultClickTimer = null;
      const nodeEl = event.target.closest('.canvas-node'); const node = nodeEl && nodeById(nodeEl.dataset.nodeId);
      if (!node) return;
      if (core.isImageNode(node)) { if (node.src) openViewer({url: node.src, title: node.name, downloadable: false, invoker: nodeEl}); return; }
      const tile = event.target.closest('.result-tile.ready'); if (!tile) return;
      const result = resultInNode(node, tile.dataset.resultId); if (!result?.artifactUrl) return;
      openViewer({url: result.artifactUrl, title: `${providerLabel(result.provider)} · ${result.modelLabel || '生成结果'}`, downloadable: true, invoker: tile});
    });
    $('#canvas-nodes').addEventListener('focusin', event => {
      const root = event.target.closest('.generation-node');
      if (!root || !event.target.closest('textarea,select,input,button,[role="radio"],[role="tab"]')) return;
      const node = nodeById(root.dataset.nodeId);
      if (node && !state.selectedIds.has(node.id)) selectNode(node.id, false, false);
    });
    $('#canvas-nodes').addEventListener('input', event => {
      const node = nodeById(event.target.closest('.canvas-node')?.dataset.nodeId);
      if (node && event.target.dataset.field === 'prompt') { node.prompt = event.target.value; syncDirty(node); scheduleSave(); scheduleLinks(); }
    });
    $('#canvas-nodes').addEventListener('change', event => {
      const node = nodeById(event.target.closest('.canvas-node')?.dataset.nodeId); if (!node) return;
      const field = event.target.dataset.field;
      if (field === 'count') node.count = core.clampCount(event.target.value);
      else if (field === 'quality') node.quality = event.target.value;
      syncDirty(node); scheduleSave();
    });
    $('#canvas-nodes').addEventListener('click', event => {
      if (suppressClick) { suppressClick = false; return; }
      const nodeEl = event.target.closest('.canvas-node'); const node = nodeEl && nodeById(nodeEl.dataset.nodeId); if (!node) return;
      if (event.target.closest('[data-delete-node]')) { removeIds([node.id]); return; }
      if (event.target.closest('[data-retry-generation]')) { retryResult(event.target.closest('[data-retry-generation]').dataset.retryGeneration, event.target.closest('[data-retry-generation]')); return; }
      if (event.target.closest('[data-reselect-image]')) { reselectImage(node); return; }
      if (!core.isGenerationNode(node)) {
        if (event.target.closest('[data-open-image]')) { openViewer({url: node.src, title: node.name, downloadable: false, invoker: nodeEl}); return; }
        if (event.detail === 0 || pointerSelectionId !== node.id) selectNode(node.id, event.shiftKey);
        pointerSelectionId = '';
        return;
      }      const batch = displayBatch(node);
      if (event.target.closest('[data-toggle-generation]')) toggleGeneration(node);
      else if (event.target.closest('[data-generate]')) generate(node);
      else if (event.target.closest('[data-open-result]')) { const primary = primaryOf(node); if (primary?.artifactUrl) openViewer({url: primary.artifactUrl, title: `${providerLabel(primary.provider)} · ${primary.modelLabel || '生成结果'}`, downloadable: true, invoker: $('[data-open-result]', nodeEl)}); }
      else if (event.target.closest('[data-download-result]')) { const primary = primaryOf(node); if (primary?.artifactUrl) downloadResult(primary.artifactUrl); }
      else if (event.target.closest('[data-sentiment]')) setSentiment(node, event.target.closest('[data-sentiment]').dataset.sentiment, event.target.closest('[data-sentiment]'));
      else if (event.target.closest('[data-remove-input]')) { const ref = event.target.closest('[data-remove-input]').dataset.removeInput; node.orderedInputIds = node.orderedInputIds.filter(item => item !== ref); syncDirty(node); render(); scheduleSave(); }
      else if (event.target.closest('[data-model-trigger]')) setPickerOpen(node, 'model', !pickerOpen(node, 'model'));
      else if (event.target.closest('[data-image-settings-trigger]')) setPickerOpen(node, 'image', !pickerOpen(node, 'image'));
      else if (event.target.closest('[data-close-picker]')) closeActivePicker(true);
      else if (event.target.closest('[data-provider-option]')) { pickerProviders.set(node.id, event.target.closest('[data-provider-option]').dataset.providerOption); render(); requestAnimationFrame(() => $(`[data-node-id="${node.id}"] [data-profile-option]:not(:disabled)`)?.focus()); }
      else if (event.target.closest('[data-profile-option]')) selectProfile(node, event.target.closest('[data-profile-option]').dataset.profileOption);
      else if (event.target.closest('[data-ratio-option]')) selectImageSetting(node, 'ratio', event.target.closest('[data-ratio-option]').dataset.ratioOption);
      else if (event.target.closest('[data-resolution-option]')) selectImageSetting(node, 'resolution', event.target.closest('[data-resolution-option]').dataset.resolutionOption);
      else {
        const tile = event.target.closest('.result-tile');
        const result = tile && batch ? resultInNode(node, tile.dataset.resultId) : null;
        /* Clicking into a textarea/select must not yank focus back to the node
           container. startNodeDrag bails out on editable targets, so
           pointerSelectionId is stale here and the fallback would otherwise
           call selectNode() with focus=true — whose rAF then focuses the
           <article tabindex="-1"> and steals the caret. Selection on that path
           is already handled by the focusin listener above (focus=false). */
        if ((event.detail === 0 || pointerSelectionId !== node.id) && !core.isEditableTarget(event.target)) selectNode(node.id, event.shiftKey);
        pointerSelectionId = '';
        if (!result) return;
        /* Debounced so a following double click opens the viewer instead. */
        clearTimeout(resultClickTimer);
        resultClickTimer = window.setTimeout(() => { resultClickTimer = null; activateResult(node, batch, result); }, resultClickDelay);
      }
    });
    let draggedInput = null; $('#canvas-nodes').addEventListener('dragstart', event => { const row = event.target.closest('[data-input-id]'); if (!row) { event.preventDefault(); return; } draggedInput = {nodeId: row.dataset.requestId, ref: row.dataset.inputId}; event.dataTransfer.effectAllowed = 'move'; }); $('#canvas-nodes').addEventListener('dragover', event => { if (draggedInput && event.target.closest('[data-input-id]')) event.preventDefault(); }); $('#canvas-nodes').addEventListener('drop', event => { const row = event.target.closest('[data-input-id]'); if (row && draggedInput) { event.preventDefault(); reorderInput(draggedInput.nodeId, draggedInput.ref, row.dataset.inputId); draggedInput = null; } }); $('#canvas-nodes').addEventListener('dragend', () => { draggedInput = null; });

    $('#context-menu').addEventListener('click', event => { const action = event.target.closest('[data-context-action]')?.dataset.contextAction; if (action) runContextAction(action); });
    $('#context-menu').addEventListener('keydown', event => { const items = $$('[role="menuitem"]:not(:disabled)', event.currentTarget); const index = items.indexOf(document.activeElement); if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus(); } else if (event.key === 'Home') items[0]?.focus(); else if (event.key === 'End') items.at(-1)?.focus(); });
    document.addEventListener('pointerdown', event => { if (!event.target.closest('#context-menu')) closeContextMenu(); if (!event.target.closest('.visual-picker')) closeActivePicker(true); });
    document.addEventListener('keydown', event => {
      const editable = core.isEditableTarget(event.target); const meta = event.ctrlKey || event.metaKey; const canvasFocused = viewport === document.activeElement || Boolean(document.activeElement?.closest?.('.canvas-node')) || Boolean(event.target.closest?.('#canvas-viewport'));
      if (event.key === 'Enter' || event.key === ' ') {
        const tile = event.target.closest?.('.result-tile');
        if (tile && !editable) { const node = nodeById(tile.closest('.canvas-node')?.dataset.nodeId); const result = node && resultInNode(node, tile.dataset.resultId); if (node && result) { event.preventDefault(); activateResult(node, displayBatch(node), result); return; } }
      }
      if (event.key === 'Escape') { let closed = cancelConnection(); closed = clearPointerInteraction() || closed; closed = closeContextMenu(true) || closed; closed = closeActivePicker() || closed; if ($('#workspace-layout').classList.contains('drawer-open')) { setProjectDrawer(false); closed = true; } if (!$('#shortcut-popover').hidden) { closeShortcut(); closed = true; } if ($('#image-viewer').open) { closeViewer(); closed = true; } if ($('#project-rename-dialog').open) { closeRenameProjectDialog(); closed = true; } closed = clearLinkSelection() || closed; if (closed) { escapeArmed = true; event.preventDefault(); return; } if (escapeArmed || state.selectedIds.size) { clearSelection(); escapeArmed = false; event.preventDefault(); } return; }
      if ($('#image-viewer').open) {
        if (editable) return;
        if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomViewer(viewerState.scale * 1.2); }
        else if (event.key === '-' || event.key === '_') { event.preventDefault(); zoomViewer(viewerState.scale / 1.2); }
        else if (event.key === '0') { event.preventDefault(); fitViewer(); }
        else if (event.key === '1') { event.preventDefault(); actualViewer(); }
        return;
      }
      if (editable) return;
      if (event.key === '?' ) { event.preventDefault(); openShortcut(); return; }
      if (meta && event.key.toLowerCase() === 'c' && canvasFocused) { event.preventDefault(); copySelection(); }
      else if (meta && event.key.toLowerCase() === 'a' && canvasFocused) { event.preventDefault(); clearLinkSelection(); state.selectedIds = new Set(state.nodes.map(node => node.id)); syncSelectionClasses(); }
      /* A selected connection wins over node selection; the two modes never
         coexist, so this single key stays unambiguous. Clicking a link leaves
         no focusable element behind, hence the explicit `selectedLinks` test. */
      else if ((event.key === 'Delete' || event.key === 'Backspace') && state.selectedLinks.size) { event.preventDefault(); removeLinks([...state.selectedLinks]); }
      else if ((event.key === 'Delete' || event.key === 'Backspace') && canvasFocused && state.selectedIds.size) { event.preventDefault(); removeIds(state.selectedIds); }
      else if (event.key === '0' && canvasFocused) { event.preventDefault(); fitView(); }
      else if ((event.key === '+' || event.key === '=') && canvasFocused) { event.preventDefault(); zoomAt(state.viewport.zoom + .1, innerWidth / 2, innerHeight / 2); }
      else if ((event.key === '-' || event.key === '_') && canvasFocused) { event.preventDefault(); zoomAt(state.viewport.zoom - .1, innerWidth / 2, innerHeight / 2); }
    });
    document.addEventListener('paste', event => {
      if (pasteEditableTarget(event.target)) return;
      const files = transferFiles(event.clipboardData);
      if (files.length) {
        event.preventDefault();
        importExternalImages(files, lastCanvasPointer || visibleWorldCenter());
      } else if (hasUrlOnlyTransfer(event.clipboardData)) {
        event.preventDefault(); toast(urlOnlyMessage, true);
      } else if (canvasClipboard?.selectedIds.length) {
        event.preventDefault(); pasteClipboard(lastCanvasPointer || visibleWorldCenter());
      }
    });
    $('#canvas-nodes').addEventListener('keydown', event => { if (!event.target.matches('[role="radio"],[role="tab"]')) return; const group = event.target.closest('[role="radiogroup"],[role="tablist"]'); const items = $$('[role="radio"],[role="tab"]', group).filter(item => !item.disabled); const index = items.indexOf(event.target); if (['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft'].includes(event.key)) { event.preventDefault(); items[(index + (['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1) + items.length) % items.length]?.focus(); } else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.target.click(); } });

    $('#mobile-projects-button').addEventListener('click', () => setProjectDrawer(!$('#workspace-layout').classList.contains('drawer-open')));
    $('#project-scrim').addEventListener('click', () => setProjectDrawer(false));
    $('#sidebar-collapse').addEventListener('click', () => setSidebarCollapsed(!$('#workspace-layout').classList.contains('sidebar-collapsed')));
    $$('[data-new-project]').forEach(button => button.addEventListener('click', createProject));
    $$('[data-rename-project-id]').forEach(button => button.addEventListener('click', () => openRenameProjectDialog(button)));
    $('#project-sidebar').addEventListener('click', event => { if (event.target.closest('a')) setProjectDrawer(false); });
    $$('[data-close-rename]').forEach(button => button.addEventListener('click', closeRenameProjectDialog)); $$('[data-close-viewer]').forEach(button => button.addEventListener('click', closeViewer)); $('[data-close-overlay]').addEventListener('click', closeShortcut);
    const viewerDialog = $('#image-viewer'); const viewerStage = $('.viewer-stage', viewerDialog);
    $('[data-view-zoom-out]', viewerDialog).addEventListener('click', () => zoomViewer(viewerState.scale / 1.2)); $('[data-view-zoom-in]', viewerDialog).addEventListener('click', () => zoomViewer(viewerState.scale * 1.2)); $('[data-view-fit]', viewerDialog).addEventListener('click', fitViewer); $('[data-view-actual]', viewerDialog).addEventListener('click', actualViewer);
    /* The viewer's download shares the result rail's PNG path instead of
       navigating to the raw artifact, so a refused conversion stays readable. */
    $('[data-view-download]', viewerDialog).addEventListener('click', event => { event.preventDefault(); downloadResult($('[data-view-download]', viewerDialog).dataset.resultUrl || ''); });
    viewerStage.addEventListener('wheel', event => { event.preventDefault(); event.stopPropagation(); zoomViewer(viewerState.scale * (event.deltaY < 0 ? 1.12 : .89), event.clientX, event.clientY); }, {passive: false});
    viewerStage.addEventListener('pointerdown', event => { if (event.button !== 0 || !viewerStage.classList.contains('can-pan')) return; event.preventDefault(); event.stopPropagation(); Object.assign(viewerState, {pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, originX: viewerState.panX, originY: viewerState.panY}); viewerStage.classList.add('is-panning'); viewerStage.setPointerCapture?.(event.pointerId); });
    viewerStage.addEventListener('pointermove', event => { if (viewerState.pointerId !== event.pointerId) return; viewerState.panX = viewerState.originX + event.clientX - viewerState.startX; viewerState.panY = viewerState.originY + event.clientY - viewerState.startY; applyViewerTransform(); });
    const endViewerPan = event => { if (viewerState.pointerId !== event.pointerId) return; viewerState.pointerId = null; viewerStage.classList.remove('is-panning'); viewerStage.releasePointerCapture?.(event.pointerId); }; viewerStage.addEventListener('pointerup', endViewerPan); viewerStage.addEventListener('pointercancel', endViewerPan);
    $('#project-rename-dialog').addEventListener('click', event => { if (event.target === event.currentTarget) closeRenameProjectDialog(); }); $('#project-rename-dialog').addEventListener('close', clearRenameProjectDialog); viewerDialog.addEventListener('click', event => { if (event.target === event.currentTarget) closeViewer(); });
    $('#minimap-toggle').addEventListener('click', () => { const svg = $('#minimap-map'); svg.hidden = !svg.hidden; $('#minimap-toggle').setAttribute('aria-expanded', String(!svg.hidden)); if (!svg.hidden) updateMinimap(); });
    $('#minimap-map').addEventListener('pointerdown', event => { event.preventDefault(); minimapDrag = event.pointerId; event.currentTarget.setPointerCapture?.(event.pointerId); recenterFromMinimap(event); }); $('#minimap-map').addEventListener('pointermove', event => { if (minimapDrag === event.pointerId) recenterFromMinimap(event); }); $('#minimap-map').addEventListener('pointerup', event => { if (minimapDrag === event.pointerId) { minimapDrag = null; scheduleSave(); } });
    window.addEventListener('resize', () => { if (!matchMedia('(max-width: 768px)').matches && $('#workspace-layout').classList.contains('drawer-open')) setProjectDrawer(false); scheduleLinks(); if (viewerDialog.open) requestAnimationFrame(viewerState.fit ? fitViewer : applyViewerTransform); });
  });
})();
