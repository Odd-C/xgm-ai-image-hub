(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ImageHubCanvasCore = Object.freeze(api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const SCHEMA_VERSION = 6;
  const IMAGE = 'image';
  const GENERATION = 'generation_node';
  const TEXT_NODE = 'text_node';
  const LEGACY_REQUEST = 'generation_request';
  const LEGACY_RESULT = 'generation_result';
  const RESULT_REF_PREFIX = 'result:';
  const DEFAULT_WIDTH = 344;
  /* 「Agent 生图器」的默认宽度：它比普通生图器多一组轴选择器（平台/市场/类目/工作类型/
     卖点 + 卖点补充），344 会把两列网格挤成一条。加宽到 420 让轴两列并排、有呼吸感。 */
  const AGENT_DEFAULT_WIDTH = 420;
  /* 卡片宽度可调（拖右下角手柄，只改宽度）。下限保证卡片里的选择器与提示词框
     不会被挤成一条，上限避免一张卡横跨整个视野。文本卡与生图卡共用这对边界。 */
  const MIN_WIDTH = 240;
  const MAX_WIDTH = 720;
  const MAX_COUNT = 4;
  /* Cards are siblings, never a grid: one card owns exactly one result. */
  const SPLIT_GAP = 120;
  const SPLIT_ROW_HEIGHT = 420;
  const SPLIT_MAX_COLUMNS = 3;

  function profileById(models, id) {
    return models.find(item => item.id === id) || null;
  }

  function chooseRequestProfile(models, inheritedProfileId) {
    if (inheritedProfileId) return profileById(models, inheritedProfileId);
    return models.find(item => item.enabled) || null;
  }

  function requestAvailability(profile) {
    if (profile?.enabled) return {enabled: true, message: ''};
    return {enabled: false, message: '当前模型不可用。请联系管理员配置或选择其他已启用模型。'};
  }

  function ratioToAspect(ratio) {
    const [width, height] = String(ratio || '4:3').split(':');
    return `${Number(width) || 4}/${Number(height) || 3}`;
  }

  function clampCount(value, fallback = 1) {
    return Math.max(1, Math.min(MAX_COUNT, Number(value) || Number(fallback) || 1));
  }

  /* 宽度是用户拖出来的，所以它必须**先被收进合法区间再存**：
     否则拖出屏幕外（或旧数据里的畸形值）会一直留在画布状态里。 */
  function clampWidth(value, fallback = DEFAULT_WIDTH) {
    const width = Number(value) || Number(fallback) || DEFAULT_WIDTH;
    return Math.round(Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, width)));
  }

  /* ---- reference identity -------------------------------------------------
     A unified node references either an uploaded/external image node id or one
     concrete result image of another unified node. The result reference keeps
     both the owning node and the immutable generation artifact identity. */

  function resultRefId(nodeId, resultId) {
    return `${RESULT_REF_PREFIX}${nodeId}:${resultId}`;
  }

  function parseResultRefId(ref) {
    if (typeof ref !== 'string' || !ref.startsWith(RESULT_REF_PREFIX)) return null;
    const rest = ref.slice(RESULT_REF_PREFIX.length);
    const separator = rest.lastIndexOf(':');
    if (separator <= 0 || separator === rest.length - 1) return null;
    return {nodeId: rest.slice(0, separator), resultId: rest.slice(separator + 1)};
  }

  function uniqueRefs(refs) {
    return [...new Set((refs || []).filter(ref => typeof ref === 'string' && ref))];
  }

  /* ---- canonical node shape ---------------------------------------------- */

  function isGenerationNode(node) {
    return Boolean(node && node.type === GENERATION);
  }

  function isImageNode(node) {
    return Boolean(node && node.type === IMAGE);
  }

  /* 文本节点：画布上的一等文本卡片。可编辑、可复制、可作为文本输出给别的节点
     （生图卡接上它，提示词就由它提供）；内置「反推提示词」—— 接了图片输入时，
     把那张图反推成一句英文提示词填进自己。
     ⚠️ 它**没有**生成能力，所以不参与 generation 的批次/结果逻辑。 */
  function isTextNode(node) {
    return Boolean(node && node.type === TEXT_NODE);
  }

  function normalizeTextNode(node) {
    return {
      ...node,
      type: TEXT_NODE,
      x: Number(node?.x) || 0,
      y: Number(node?.y) || 0,
      width: clampWidth(node?.width),
      text: typeof node?.text === 'string' ? node.text : '',
      // 图片输入（来自图片节点或某个生成结果）—— 反推提示词的原料。
      orderedInputIds: uniqueRefs(node?.orderedInputIds),
    };
  }

  /* 提示词来源：生图卡接上文本卡后，`promptSourceId` 指向那张卡，
     提示词就是它的镜像。指向已经不存在的节点（或指向的不是文本卡）时按没有处理。 */
  function promptSourceNode(nodes, node) {
    if (!isGenerationNode(node) || !node.promptSourceId) return null;
    const source = (nodes || []).find(item => item.id === node.promptSourceId);
    return isTextNode(source) ? source : null;
  }

  /* ---- 连线的唯一判据 -------------------------------------------------------
     画布上只有两种落点：生图卡的输入端口（接参考图，也能接一张文本卡当提示词来源）
     和文本卡的输入端口（接参考图，那是「反推提示词」的原料）。
     渲染连线、高亮落点、真正落线三处都问这里，避免各写一份规则后互相不一致。 */

  function canLink(nodes, target, sourceId) {
    if (!target || !sourceId) return false;
    if (parseResultRefId(sourceId)) return isGenerationNode(target) || isTextNode(target);
    const source = (nodes || []).find(node => node.id === sourceId);
    if (isTextNode(source)) return isGenerationNode(target);
    return isImageNode(source) && (isGenerationNode(target) || isTextNode(target));
  }

  function hasLiveLink(target, sourceId) {
    if (!target || !sourceId) return false;
    if ((target.orderedInputIds || []).includes(sourceId)) return true;
    return isGenerationNode(target) && target.promptSourceId === sourceId;
  }

  function normalizeResult(result) {
    const parameters = result?.parameters && typeof result.parameters === 'object' ? {...result.parameters} : {};
    return {
      id: result?.id || '',
      generationId: result?.generationId || '',
      artifactUrl: result?.artifactUrl || '',
      status: result?.status || 'queued',
      error: result?.error || '',
      canRetry: Boolean(result?.canRetry),
      sentiment: result?.sentiment || '',
      aspect: result?.aspect || ratioToAspect(parameters.ratio),
      provider: result?.provider || '',
      modelLabel: result?.modelLabel || '',
      prompt: result?.prompt || '',
      optimized_prompt: result?.optimized_prompt || '',
      effective_prompt: result?.effective_prompt || '',
      prompt_optimize_state: result?.prompt_optimize_state || 'none',
      prompt_optimize_error: result?.prompt_optimize_error || '',
      prompt_optimize_mode: result?.prompt_optimize_mode || '',
      prompt_optimize_note: result?.prompt_optimize_note || '',
      profileId: result?.profileId || '',
      parameters,
      createdAt: result?.createdAt || '',
    };
  }

  function normalizeRequest(request) {
    return {
      prompt: request?.prompt || '',
      profileId: request?.profileId || '',
      provider: request?.provider || '',
      ratio: request?.ratio || '1:1',
      resolution: request?.resolution || '2K',
      quality: request?.quality || 'standard',
      count: clampCount(request?.count),
      orderedInputIds: uniqueRefs(request?.orderedInputIds),
      agentSpec: request?.agentSpec && typeof request.agentSpec === 'object' ? {...request.agentSpec} : null,
    };
  }

  function normalizeBatch(batch, options = {}) {
    if (!batch || typeof batch !== 'object') return null;
    const results = Array.isArray(batch.results) ? batch.results.map(normalizeResult) : [];
    if (!results.length) return null;
    const normalized = {
      id: batch.id || '',
      createdAt: batch.createdAt || '',
      parentGenerationId: batch.parentGenerationId || '',
      request: normalizeRequest(batch.request),
      results,
    };
    if (options.withAttemptState) {
      normalized.settled = Boolean(batch.settled);
      normalized.promoted = Boolean(batch.promoted);
    }
    return normalized;
  }

  function normalizeGenerationNode(node) {
    const normalized = {
      ...node,
      type: GENERATION,
      x: Number(node?.x) || 0,
      y: Number(node?.y) || 0,
      width: clampWidth(node?.width, node?.agentSpec ? AGENT_DEFAULT_WIDTH : DEFAULT_WIDTH),
      expanded: node?.expanded !== false,
      /* A user-initiated expand is pinned: background result refreshes may
         re-render the card but must never collapse it again. Only the header
         toggle (or submitting a new generation) clears the pin. */
      expandedPinned: node?.expandedPinned === true,
      prompt: node?.prompt || '',
      /* 提示词来源：一个文本节点的 id。设了它，`prompt` 就是那张文本卡的镜像
         （改文本卡即时写回这里，卡片自己的提示词框随之只读），断开连线时用
         `promptBackup` 把用户连线前手写的内容还回去。 */
      promptSourceId: node?.promptSourceId || '',
      promptBackup: typeof node?.promptBackup === 'string' ? node.promptBackup : '',
      provider: node?.provider || '',
      profileId: node?.profileId || '',
      ratio: node?.ratio || '1:1',
      resolution: node?.resolution || '2K',
      quality: node?.quality || 'standard',
      count: clampCount(node?.count),
      /* 「Agent 生图器」：同一张生成卡，但带上结构化轴（平台/市场/类目/工作类型/卖点）。
         存在即代表这是 agent 节点 —— 走 API 渠道、由服务端按平台规范推导比例，
         提交时会带 agent_spec。空对象/未设置 = 普通生图节点。 */
      agentSpec: node?.agentSpec && typeof node.agentSpec === 'object' ? {...node.agentSpec} : null,
      /* 逐张参考图的职责：`{<输入引用>: <职责 key>}`。**允许留空**（键缺失 = 未标注），
         提交时按输入顺序排成 reference_roles，空位留空串由服务端/模型补。 */
      agentRoles: node?.agentRoles && typeof node.agentRoles === 'object' ? {...node.agentRoles} : {},
      orderedInputIds: uniqueRefs(node?.orderedInputIds || node?.referenceOrder),
      parentGenerationId: node?.parentGenerationId || '',
      primaryResultId: node?.primaryResultId || '',
      error: node?.error || '',
      contentMode: node?.contentMode || 'created',
      profileUnavailable: Boolean(node?.profileUnavailable),
      submitting: false,
      uploading: false,
      activeBatch: normalizeBatch(node?.activeBatch),
      attempt: normalizeBatch(node?.attempt, {withAttemptState: true}),
    };
    if (normalized.attempt) {
      const results = normalized.attempt.results;
      normalized.attempt.settled = results.every(result => !['queued', 'running'].includes(result.status));
    }
    normalized.dirty = isDirty(normalized);
    return normalized;
  }

  function batchResults(batch) {
    return Array.isArray(batch?.results) ? batch.results : [];
  }

  function nodeResults(node) {
    return [...batchResults(node?.activeBatch), ...batchResults(node?.attempt)];
  }

  function findResult(nodes, generationId) {
    if (!generationId) return null;
    for (const node of nodes || []) {
      if (!isGenerationNode(node)) continue;
      const result = nodeResults(node).find(item => item.generationId === generationId);
      if (result) return {node, result};
    }
    return null;
  }

  function requestSnapshot(node) {
    return normalizeRequest({
      prompt: node?.prompt || '',
      profileId: node?.profileId || '',
      provider: node?.provider || '',
      ratio: node?.ratio || '',
      resolution: node?.resolution || '',
      quality: node?.quality || '',
      count: node?.count,
      orderedInputIds: node?.orderedInputIds,
      agentSpec: node?.agentSpec,
    });
  }

  function activeRequest(node) {
    return node?.attempt?.request || node?.activeBatch?.request || null;
  }

  function isDirty(node) {
    const request = activeRequest(node);
    if (!request || !isGenerationNode(node)) return false;
    const current = requestSnapshot(node);
    return current.prompt !== request.prompt
      || current.profileId !== request.profileId
      || current.provider !== request.provider
      || current.ratio !== request.ratio
      || current.resolution !== request.resolution
      || current.quality !== request.quality
      || current.count !== request.count
      || current.orderedInputIds.join('|') !== request.orderedInputIds.join('|');
  }

  function primaryResult(node) {
    const results = batchResults(node?.activeBatch);
    if (!results.length) return null;
    return results.find(result => result.id === node.primaryResultId)
      || results.find(result => result.status === 'succeeded')
      || results[0];
  }

  function primaryGenerationId(node) {
    return primaryResult(node)?.generationId || node?.parentGenerationId || '';
  }

  function nodeStatus(node) {
    if (node?.submitting || (node?.attempt && !node.attempt.settled)) return 'processing';
    const results = batchResults(node?.activeBatch);
    if (!results.length) return node?.error ? 'failed' : 'empty';
    const succeeded = results.filter(result => result.status === 'succeeded').length;
    if (succeeded && succeeded === results.length) return 'succeeded';
    if (succeeded) return 'partial';
    if (results.some(result => result.status === 'recovery_required')) return 'recovery_required';
    return 'failed';
  }

  function batchProgress(node) {
    const attempt = node?.attempt;
    const results = batchResults(attempt);
    if (!attempt || !results.length) return null;
    const done = results.filter(result => !['queued', 'running'].includes(result.status)).length;
    return {done, total: results.length, settled: Boolean(attempt.settled)};
  }

  function attemptError(node) {
    const attempt = node?.attempt;
    if (!attempt || !attempt.settled || attempt.promoted) return '';
    return batchResults(attempt).map(result => result.error).find(Boolean) || '';
  }

  /* ---- references --------------------------------------------------------- */

  function resolveInputRef(nodes, ref) {
    const parsed = parseResultRefId(ref);
    if (parsed) {
      const node = (nodes || []).find(item => item.id === parsed.nodeId);
      if (!isGenerationNode(node)) return null;
      const result = nodeResults(node).find(item => item.id === parsed.resultId);
      if (!result || !result.artifactUrl) return null;
      return {id: ref, kind: 'result', node, result, src: result.artifactUrl, name: `${result.modelLabel || '生成结果'} · ${result.parameters?.ratio || ''}`.trim(), generationId: result.generationId || '', localOnly: false};
    }
    const node = (nodes || []).find(item => item.id === ref);
    if (!isImageNode(node)) return null;
    return {id: ref, kind: 'image', node, result: null, src: node.src || '', name: node.name || '参考图片', generationId: '', localOnly: Boolean(node.localOnly)};
  }

  function resolveInputRefs(nodes, node) {
    return (node?.orderedInputIds || []).map(ref => ({ref, source: resolveInputRef(nodes, ref)}));
  }

  function firstMissingLocalInput(nodes, node, hasLocalFile) {
    for (const ref of node?.orderedInputIds || []) {
      const source = resolveInputRef(nodes, ref);
      if (source?.kind === 'image' && source.localOnly && !hasLocalFile(ref)) return source.node;
    }
    return null;
  }

  /* ---- one card per result -------------------------------------------------
     A unified card used to show a whole batch as an inner grid, which made a
     single image impossible to download on its own. After a batch settles it is
     split into sibling cards: the submitting card keeps result #1 (the user's
     context never jumps) and every other result moves into its own card that
     carries the same recipe. Splitting is presentation-only: each result stays
     an independent immutable Generation, references are remapped, and no
     evidence is created, copied or dropped. */

  function splitCandidateBatch(node) {
    if (!isGenerationNode(node)) return null;
    /* A running attempt owns the card; splitting waits until it settles. */
    if (node.attempt && !node.attempt.settled) return null;
    const batches = batchResults(node.activeBatch).length ? [node.activeBatch] : [node.attempt];
    for (const batch of batches) {
      const results = batchResults(batch);
      if (results.length <= 1) continue;
      if (results.some(result => ['queued', 'running'].includes(result.status))) continue;
      if (!results.some(result => result.status === 'succeeded')) continue;
      return batch;
    }
    return null;
  }

  /* Right of the origin card first, wrapping into tidy rows afterwards. With a
     visible world rect the grid stays inside the viewport: when the right side
     is used up the sequence continues on the row below the origin card instead
     of covering it. Positions are computed once and then persisted, so a
     settled layout never drifts. */
  function splitGrid(origin, count, options = {}) {
    const gap = Math.max(0, Number(options.gap ?? SPLIT_GAP)) || SPLIT_GAP;
    const width = Number(origin?.width) || DEFAULT_WIDTH;
    const rawRow = typeof options.rowHeight === 'function' ? options.rowHeight(origin) : options.rowHeight;
    const rowHeight = Math.max(160, Number(rawRow) || SPLIT_ROW_HEIGHT);
    const visible = options.visible && Number.isFinite(Number(options.visible.width)) ? options.visible : null;
    const originX = Number(origin?.x) || 0;
    const originY = Number(origin?.y) || 0;
    let columns = Math.max(1, Math.min(SPLIT_MAX_COLUMNS, Number(options.columns) || SPLIT_MAX_COLUMNS));
    let startX = originX + width + gap;
    let startY = originY;
    if (visible) {
      const right = Number(visible.x) + Number(visible.width);
      const room = Math.floor((right - startX + gap) / (width + gap));
      columns = Math.max(1, Math.min(columns, room || 1));
      if (room < 1) {
        startX = Math.min(originX, Math.max(Number(visible.x), right - width));
        startY = originY + rowHeight + gap;
      }
    }
    return Array.from({length: count}, (_, index) => ({
      x: startX + (index % columns) * (width + gap),
      y: startY + Math.floor(index / columns) * (rowHeight + gap),
    }));
  }

  function splitRecipe(node) {
    return normalizeRequest({
      prompt: node?.prompt,
      profileId: node?.profileId,
      provider: node?.provider,
      ratio: node?.ratio,
      resolution: node?.resolution,
      quality: node?.quality,
      count: 1,
      orderedInputIds: node?.orderedInputIds,
    });
  }

  /* Idempotent: single-result cards pass through untouched, so a reload never
     splits twice. Returns the node list plus how many origin cards were split. */
  function splitGenerationNodes(nodes, options = {}) {
    const list = Array.isArray(nodes) ? nodes : [];
    const remap = new Map();
    const created = [];
    let splits = 0;
    list.forEach(node => {
      const batch = splitCandidateBatch(node);
      if (!batch) return;
      const results = batchResults(batch);
      const [first, ...rest] = results;
      const recipe = splitRecipe(node);
      const nextBatch = {...batch, request: recipe, results: [first]};
      node.activeBatch = nextBatch;
      if (batch === node.attempt) node.attempt = null;
      node.count = 1;
      node.primaryResultId = first.id;
      node.dirty = isDirty(node);
      const positions = splitGrid(node, rest.length, options);
      /* 提示词来源跟着一起拆：文本卡连着原卡时，每张新卡都该继续由它供词，
         而不是各自留一份抄下来的字。来源已经不在（或不是文本卡）就按没有处理，
         免得留下一串指向空气的 promptSourceId。 */
      const textSource = promptSourceNode(list, node);
      rest.forEach((result, index) => {
        const id = options.uid ? options.uid('generation') : `${node.id}-result-${index + 2}`;
        remap.set(`${node.id}:${result.id}`, id);
        created.push(normalizeGenerationNode({
          id,
          type: GENERATION,
          x: positions[index].x,
          y: positions[index].y,
          width: node.width,
          expanded: false,
          expandedPinned: false,
          contentMode: node.contentMode,
          profileUnavailable: node.profileUnavailable,
          error: '',
          parentGenerationId: node.parentGenerationId,
          ...recipe,
          promptSourceId: textSource ? textSource.id : '',
          /* 断开连线时回到拆分时那份文本，而不是一个空框 —— 这张卡没有更早的
             手写历史，原卡的 promptBackup 是**那张卡**的往事，不该搬过来。 */
          promptBackup: textSource ? recipe.prompt : '',
          activeBatch: {
            id: `${batch.id || id}-split-${index + 2}`,
            createdAt: batch.createdAt,
            parentGenerationId: batch.parentGenerationId,
            request: recipe,
            results: [result],
          },
          primaryResultId: result.id,
        }));
      });
      splits += 1;
    });
    if (!splits) return {nodes: list, created: [], changed: false, splits: 0};
    const all = [...list, ...created];
    if (remap.size) {
      /* A moved result keeps its identity, so every reference that pointed at it
         is re-pointed at the card that now holds it. */
      all.forEach(node => {
        if (!isGenerationNode(node) || !(node.orderedInputIds || []).length) return;
        node.orderedInputIds = node.orderedInputIds.map(ref => {
          const parsed = parseResultRefId(ref);
          const target = parsed ? remap.get(`${parsed.nodeId}:${parsed.resultId}`) : '';
          return target ? resultRefId(target, parsed.resultId) : ref;
        });
      });
    }
    return {nodes: all, created, changed: true, splits};
  }

  /* ---- legacy canvas migration ------------------------------------------- */

  function legacyResultToResult(result) {
    return normalizeResult({
      id: result.id,
      generationId: result.generationId || '',
      artifactUrl: result.artifactUrl || '',
      status: result.status || 'queued',
      error: result.error || '',
      canRetry: result.canRetry,
      sentiment: result.sentiment || '',
      aspect: result.aspect || ratioToAspect(result.parameters?.ratio),
      provider: result.provider || '',
      modelLabel: result.modelLabel || '',
      prompt: result.prompt || '',
      profileId: result.profileId || '',
      parameters: result.parameters,
      createdAt: result.createdAt || '',
    });
  }

  function batchRequestFromNode(node, count, orderedInputIds) {
    return normalizeRequest({
      prompt: node?.prompt,
      profileId: node?.profileId,
      provider: node?.provider,
      ratio: node?.ratio,
      resolution: node?.resolution,
      quality: node?.quality,
      count,
      orderedInputIds,
    });
  }

  /* Idempotent: unified nodes pass through untouched, legacy request/result
     presentation nodes merge exactly once, and every card that still held a
     multi-image grid is split into one card per result. */
  function migrateCanvasNodes(nodes, options = {}) {
    const list = (Array.isArray(nodes) ? nodes : []).filter(node => node && typeof node === 'object');
    const legacy = list.filter(node => node.type === LEGACY_REQUEST || node.type === LEGACY_RESULT);
    if (!legacy.length) {
      const passthrough = list.map(node => ({...node}));
      const split = splitGenerationNodes(passthrough, options);
      return {nodes: split.nodes, migrated: split.changed};
    }

    const plan = new Map();
    list.forEach(node => { if (node.type === LEGACY_REQUEST) plan.set(node.id, {request: node, results: []}); });
    const orphans = [];
    list.forEach(node => {
      if (node.type !== LEGACY_RESULT) return;
      const owner = node.requestId ? plan.get(node.requestId) : null;
      if (owner) owner.results.push(node); else orphans.push(node);
    });
    orphans.forEach((result, index) => plan.set(`generation-${result.id}`, {
      request: null,
      results: [result],
      fallback: {x: 180 + (index % 3) * 300, y: 150 + Math.floor(index / 3) * 380},
    }));

    const refByLegacyResultId = new Map();
    // A multi-result legacy request is split so each card owns exactly one
    // result. Only the first result stays on the original node; the rest are
    // re-homed onto `${nodeId}-result-${index + 1}`, which is the same naming
    // rule splitGenerationNodes uses below. References must be remapped to the
    // node that will actually own the result, not the node they came from.
    plan.forEach((entry, nodeId) => entry.results.forEach((result, index) => {
      const ownerId = index === 0 ? nodeId : `${nodeId}-result-${index + 1}`;
      refByLegacyResultId.set(result.id, resultRefId(ownerId, result.id));
    }));
    const remapRefs = refs => uniqueRefs((refs || []).map(ref => refByLegacyResultId.get(ref) || ref));

    const migratedNodes = [];
    list.forEach(node => {
      if (node.type === LEGACY_RESULT) return;
      if (node.type === LEGACY_REQUEST) {
        const entry = plan.get(node.id) || {request: node, results: []};
        const orderedInputIds = remapRefs(node.orderedInputIds || node.referenceOrder);
        const results = entry.results.map(legacyResultToResult);
        const count = results.length ? clampCount(node.count, results.length) : clampCount(node.count);
        migratedNodes.push(normalizeGenerationNode({
          ...node,
          type: GENERATION,
          orderedInputIds,
          count,
          expanded: results.length ? false : node.expanded !== false,
          contentMode: 'migrated',
          activeBatch: results.length ? {
            id: `batch-${node.id}`,
            createdAt: '',
            parentGenerationId: node.parentGenerationId || '',
            request: batchRequestFromNode(node, count, orderedInputIds),
            results,
          } : null,
          primaryResultId: results.length ? (results.find(result => result.status === 'succeeded') || results[0]).id : '',
        }));
        return;
      }
      migratedNodes.push({...node});
    });

    orphans.forEach((result, index) => {
      const nodeId = `generation-${result.id}`;
      const entry = plan.get(nodeId);
      const single = legacyResultToResult(result);
      const request = normalizeRequest({
        prompt: single.prompt,
        profileId: single.profileId,
        provider: single.provider,
        ratio: single.parameters.ratio || '4:3',
        resolution: single.parameters.resolution || '2K',
        quality: single.parameters.quality || 'standard',
        count: 1,
        orderedInputIds: [],
      });
      migratedNodes.push(normalizeGenerationNode({
        id: nodeId,
        type: GENERATION,
        x: entry.fallback.x,
        y: entry.fallback.y,
        width: DEFAULT_WIDTH,
        expanded: false,
        contentMode: 'historical',
        ...request,
        parentGenerationId: single.generationId && result.parentGenerationId ? result.parentGenerationId : '',
        activeBatch: {id: `batch-${nodeId}`, createdAt: single.createdAt, parentGenerationId: result.parentGenerationId || '', request, results: [single]},
        primaryResultId: single.id,
        profileUnavailable: Boolean(result.profileUnavailable),
      }));
    });

    const split = splitGenerationNodes(migratedNodes, options);
    return {nodes: split.nodes, migrated: true};
  }

  /* ---- history ----------------------------------------------------------- */

  function historicalNode(item, uid) {
    const parameters = item.parameters || {};
    const request = normalizeRequest({
      prompt: item.prompt,
      profileId: item.profile_id,
      provider: item.provider,
      ratio: parameters.ratio || '4:3',
      resolution: parameters.resolution || '2K',
      quality: parameters.quality || 'standard',
      count: 1,
      orderedInputIds: [],
    });
    const result = normalizeResult({
      id: uid('result'),
      generationId: item.id,
      artifactUrl: item.artifact_url,
      status: item.status,
      canRetry: item.can_retry,
      sentiment: item.sentiment || '',
      aspect: ratioToAspect(parameters.ratio),
      provider: item.provider,
      modelLabel: item.model_label,
      prompt: item.prompt,
      optimized_prompt: item.optimized_prompt || '',
      effective_prompt: item.effective_prompt || '',
      prompt_optimize_state: item.prompt_optimize_state || 'none',
      prompt_optimize_error: item.prompt_optimize_error || '',
      prompt_optimize_mode: item.prompt_optimize_mode || '',
      prompt_optimize_note: item.prompt_optimize_note || '',
      profileId: item.profile_id || '',
      parameters,
      createdAt: item.created_at || '',
    });
    return normalizeGenerationNode({
      id: uid('generation'),
      type: GENERATION,
      x: 180,
      y: 150,
      width: DEFAULT_WIDTH,
      expanded: false,
      contentMode: 'historical',
      ...request,
      profileUnavailable: item.profile_available === false,
      parentGenerationId: item.parent_generation_id || '',
      activeBatch: {id: uid('batch'), createdAt: item.created_at || '', parentGenerationId: item.parent_generation_id || '', request, results: [result]},
      primaryResultId: result.id,
    });
  }

  function historyAction(nodes, item, action, uid) {
    if (action !== 'locate' && action !== 'continue') throw new Error(`Unsupported history action: ${action}`);
    let found = findResult(nodes, item.id);
    let createdNode = false;
    if (!found) {
      const node = historicalNode(item, uid);
      nodes.push(node);
      found = {node, result: node.activeBatch.results[0]};
      createdNode = true;
    }
    const resultRef = resultRefId(found.node.id, found.result.id);
    if (action === 'locate') {
      return {node: found.node, result: found.result, resultRef, createdNode, focusId: found.node.id};
    }
    const parameters = item.parameters || {};
    return {
      node: found.node,
      result: found.result,
      resultRef,
      createdNode,
      focusId: '',
      create: {
        x: found.node.x + found.node.width + 120,
        y: found.node.y,
        prompt: item.prompt,
        provider: item.provider,
        profileId: item.profile_id || '',
        ratio: parameters.ratio || found.node.ratio,
        resolution: parameters.resolution || found.node.resolution,
        quality: parameters.quality || found.node.quality,
        count: 1,
        orderedInputIds: [resultRef],
        parentGenerationId: item.id,
        expanded: true,
      },
    };
  }

  /* ---- restore, clone, delete -------------------------------------------- */

  function restoreCanvasNodes(nodes) {
    return (Array.isArray(nodes) ? nodes : []).map(node => {
      if (!node || typeof node !== 'object') return node;
      if (node.type === IMAGE) {
        const restored = {...node};
        if (restored.localOnly) { restored.src = ''; restored.needsReselect = true; }
        return restored;
      }
      if (node.type === GENERATION) return normalizeGenerationNode(node);
      if (node.type === TEXT_NODE) return normalizeTextNode(node);
      const restored = {...node};
      if (restored.type === LEGACY_REQUEST) restored.orderedInputIds = [...(restored.orderedInputIds || restored.referenceOrder || [])];
      return restored;
    });
  }

  function isPresentationNode(node) {
    return Boolean(node && [IMAGE, GENERATION, TEXT_NODE, LEGACY_REQUEST, LEGACY_RESULT].includes(node.type));
  }

  function refOwnerNodeId(ref) {
    const parsed = parseResultRefId(ref);
    return parsed ? parsed.nodeId : ref;
  }

  function clonePresentationNodes(nodes, selectedIds, uid, offset = {x: 36, y: 36}) {
    const selected = new Set(selectedIds);
    const sourceById = new Map(nodes.map(node => [node.id, node]));
    const copied = nodes.filter(node => selected.has(node.id) && isPresentationNode(node));
    const prefix = type => type === IMAGE ? 'image' : type === TEXT_NODE ? 'text' : 'generation';
    const idMap = new Map(copied.map(node => [node.id, uid(prefix(node.type))]));
    /* 一个引用（图片 id 或结果引用）指向的节点也被一起复制时，克隆要连到克隆，
       否则两份副本会共享同一张图；没被复制的就保持原引用。 */
    const remapRefs = refs => uniqueRefs(refs).flatMap(ref => {
      const ownerId = refOwnerNodeId(ref);
      const owner = sourceById.get(ownerId);
      if (parseResultRefId(ref) && selected.has(ownerId)) return [];
      if (!parseResultRefId(ref) && owner && !selected.has(ownerId)) return [ref];
      return [idMap.get(ownerId) || ref];
    });
    const clones = copied.map(node => {
      const clone = {...node, id: idMap.get(node.id), x: Number(node.x || 0) + offset.x, y: Number(node.y || 0) + offset.y};
      if (clone.type === IMAGE) {
        clone.needsReselect = Boolean(node.needsReselect);
        return clone;
      }
      if (clone.type === TEXT_NODE) {
        clone.orderedInputIds = remapRefs(node.orderedInputIds);
        return normalizeTextNode(clone);
      }
      /* A duplicate carries the recipe, never fabricated generation evidence. */
      clone.orderedInputIds = remapRefs(node.orderedInputIds);
      /* 提示词来源也在选区里就连到它的克隆；否则把文本落成手写提示词，
         副本不该悄悄绑回原卡、让两张卡的字一起变。 */
      if (node.promptSourceId) {
        if (selected.has(node.promptSourceId)) clone.promptSourceId = idMap.get(node.promptSourceId) || '';
        else {
          clone.promptSourceId = '';
          clone.promptBackup = '';
          const owner = sourceById.get(node.promptSourceId);
          if (isTextNode(owner)) clone.prompt = owner.text || '';
        }
      }
      clone.expanded = true;
      clone.expandedPinned = false;
      clone.submitting = false;
      clone.uploading = false;
      clone.error = '';
      clone.dirty = false;
      /* The recipe lives on the batch request, so lift its content fields onto
         the clone before the batch itself is dropped. orderedInputIds stays as
         remapped above, therefore it is deliberately not copied from the recipe. */
      const recipe = node.activeBatch?.request || node.attempt?.request || null;
      if (recipe) {
        for (const field of ['prompt', 'profileId', 'provider', 'ratio', 'resolution', 'quality']) {
          if (recipe[field] !== undefined && recipe[field] !== null) clone[field] = recipe[field];
        }
      }
      clone.activeBatch = null;
      clone.attempt = null;
      clone.primaryResultId = '';
      clone.contentMode = 'created';
      return normalizeGenerationNode(clone);
    });
    return {clones, idMap: Object.fromEntries(idMap)};
  }

  function removePresentationNodes(nodes, selectedIds) {
    const removed = new Set(selectedIds);
    return nodes.filter(node => !removed.has(node.id)).map(node => {
      if (node.type === IMAGE) return node;
      // 文本节点也有输入（图片），删掉源图时同样要清理它的引用。
      if (node.type !== GENERATION && node.type !== LEGACY_REQUEST && node.type !== TEXT_NODE) return node;
      const next = {...node, orderedInputIds: (node.orderedInputIds || []).filter(ref => !removed.has(refOwnerNodeId(ref)))};
      /* 提供提示词的文本卡被删掉时连线必须一起消失，否则卡片会留着一个指向空气的来源。
         字已经镜像在 `prompt` 上，所以用户不会丢内容。 */
      if (next.promptSourceId && removed.has(next.promptSourceId)) next.promptSourceId = '';
      return next;
    });
  }

  /* ---- geometry ---------------------------------------------------------- */

  function rectCenterToWorld(elementRect, viewportRect, viewport) {
    return {
      x: (elementRect.left + elementRect.width / 2 - viewportRect.left - viewport.x) / viewport.zoom,
      y: (elementRect.top + elementRect.height / 2 - viewportRect.top - viewport.y) / viewport.zoom,
    };
  }

  function visibleWorld(viewportRect, viewport) {
    return {x: -viewport.x / viewport.zoom, y: -viewport.y / viewport.zoom, width: viewportRect.width / viewport.zoom, height: viewportRect.height / viewport.zoom};
  }

  function minimapGeometry(rects, visible, size = {width: 176, height: 112}, padding = 8) {
    const all = [...rects, visible];
    const margin = 80;
    const minX = Math.min(...all.map(rect => rect.x)) - margin;
    const minY = Math.min(...all.map(rect => rect.y)) - margin;
    const maxX = Math.max(...all.map(rect => rect.x + rect.width)) + margin;
    const maxY = Math.max(...all.map(rect => rect.y + rect.height)) + margin;
    const worldWidth = Math.max(1, maxX - minX);
    const worldHeight = Math.max(1, maxY - minY);
    const scale = Math.min((size.width - padding * 2) / worldWidth, (size.height - padding * 2) / worldHeight);
    const ox = (size.width - worldWidth * scale) / 2 - minX * scale;
    const oy = (size.height - worldHeight * scale) / 2 - minY * scale;
    const mapRect = rect => ({x: rect.x * scale + ox, y: rect.y * scale + oy, width: Math.max(2, rect.width * scale), height: Math.max(2, rect.height * scale)});
    return {bounds: {x: minX, y: minY, width: worldWidth, height: worldHeight}, scale, ox, oy, nodes: rects.map(mapRect), viewport: mapRect(visible)};
  }

  function minimapPointToWorld(point, geometry) {
    return {x: (point.x - geometry.ox) / geometry.scale, y: (point.y - geometry.oy) / geometry.scale};
  }

  function isEditableTarget(target) {
    if (!target) return false;
    const tag = String(target.tagName || '').toLowerCase();
    return ['input', 'textarea', 'select'].includes(tag) || Boolean(target.isContentEditable || target.closest?.('[contenteditable="true"]'));
  }

  return {
    SCHEMA_VERSION, IMAGE, GENERATION, TEXT_NODE, LEGACY_REQUEST, LEGACY_RESULT, DEFAULT_WIDTH, AGENT_DEFAULT_WIDTH, MIN_WIDTH, MAX_WIDTH, MAX_COUNT,
    SPLIT_GAP, SPLIT_ROW_HEIGHT, SPLIT_MAX_COLUMNS,
    profileById, chooseRequestProfile, requestAvailability, ratioToAspect, clampCount, clampWidth,
    resultRefId, parseResultRefId, uniqueRefs,
    isGenerationNode, isImageNode, isTextNode, normalizeTextNode, promptSourceNode, canLink, hasLiveLink, normalizeGenerationNode, normalizeBatch, normalizeResult, normalizeRequest,
    batchResults, nodeResults, findResult, requestSnapshot, activeRequest, isDirty,
    primaryResult, primaryGenerationId, nodeStatus, batchProgress, attemptError,
    resolveInputRef, resolveInputRefs, firstMissingLocalInput,
    splitCandidateBatch, splitGrid, splitGenerationNodes,
    migrateCanvasNodes, historyAction, historicalNode,
    restoreCanvasNodes, isPresentationNode, clonePresentationNodes, removePresentationNodes,
    rectCenterToWorld, visibleWorld, minimapGeometry, minimapPointToWorld, isEditableTarget,
  };
});
