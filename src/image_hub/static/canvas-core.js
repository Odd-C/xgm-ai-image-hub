(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ImageHubCanvasCore = Object.freeze(api);
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const SCHEMA_VERSION = 6;
  const IMAGE = 'image';
  const GENERATION = 'generation_node';
  const LEGACY_REQUEST = 'generation_request';
  const LEGACY_RESULT = 'generation_result';
  const RESULT_REF_PREFIX = 'result:';
  const DEFAULT_WIDTH = 344;
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
      width: Number(node?.width) || DEFAULT_WIDTH,
      expanded: node?.expanded !== false,
      /* A user-initiated expand is pinned: background result refreshes may
         re-render the card but must never collapse it again. Only the header
         toggle (or submitting a new generation) clears the pin. */
      expandedPinned: node?.expandedPinned === true,
      prompt: node?.prompt || '',
      provider: node?.provider || '',
      profileId: node?.profileId || '',
      ratio: node?.ratio || '1:1',
      resolution: node?.resolution || '2K',
      quality: node?.quality || 'standard',
      count: clampCount(node?.count),
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
      const restored = {...node};
      if (restored.type === LEGACY_REQUEST) restored.orderedInputIds = [...(restored.orderedInputIds || restored.referenceOrder || [])];
      return restored;
    });
  }

  function isPresentationNode(node) {
    return Boolean(node && [IMAGE, GENERATION, LEGACY_REQUEST, LEGACY_RESULT].includes(node.type));
  }

  function refOwnerNodeId(ref) {
    const parsed = parseResultRefId(ref);
    return parsed ? parsed.nodeId : ref;
  }

  function clonePresentationNodes(nodes, selectedIds, uid, offset = {x: 36, y: 36}) {
    const selected = new Set(selectedIds);
    const sourceById = new Map(nodes.map(node => [node.id, node]));
    const copied = nodes.filter(node => selected.has(node.id) && isPresentationNode(node));
    const idMap = new Map(copied.map(node => [node.id, uid(node.type === IMAGE ? 'image' : 'generation')]));
    const clones = copied.map(node => {
      const clone = {...node, id: idMap.get(node.id), x: Number(node.x || 0) + offset.x, y: Number(node.y || 0) + offset.y};
      if (clone.type === IMAGE) {
        clone.needsReselect = Boolean(node.needsReselect);
        return clone;
      }
      /* A duplicate carries the recipe, never fabricated generation evidence. */
      clone.orderedInputIds = uniqueRefs(node.orderedInputIds).flatMap(ref => {
        const ownerId = refOwnerNodeId(ref);
        const owner = sourceById.get(ownerId);
        if (parseResultRefId(ref) && selected.has(ownerId)) return [];
        if (!parseResultRefId(ref) && owner && !selected.has(ownerId)) return [ref];
        return [idMap.get(ownerId) || ref];
      });
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
      if (node.type !== GENERATION && node.type !== LEGACY_REQUEST) return node;
      return {...node, orderedInputIds: (node.orderedInputIds || []).filter(ref => !removed.has(refOwnerNodeId(ref)))};
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
    SCHEMA_VERSION, IMAGE, GENERATION, LEGACY_REQUEST, LEGACY_RESULT, DEFAULT_WIDTH, MAX_COUNT,
    SPLIT_GAP, SPLIT_ROW_HEIGHT, SPLIT_MAX_COLUMNS,
    profileById, chooseRequestProfile, requestAvailability, ratioToAspect, clampCount,
    resultRefId, parseResultRefId, uniqueRefs,
    isGenerationNode, isImageNode, normalizeGenerationNode, normalizeBatch, normalizeResult, normalizeRequest,
    batchResults, nodeResults, findResult, requestSnapshot, activeRequest, isDirty,
    primaryResult, primaryGenerationId, nodeStatus, batchProgress, attemptError,
    resolveInputRef, resolveInputRefs, firstMissingLocalInput,
    splitCandidateBatch, splitGrid, splitGenerationNodes,
    migrateCanvasNodes, historyAction, historicalNode,
    restoreCanvasNodes, isPresentationNode, clonePresentationNodes, removePresentationNodes,
    rectCenterToWorld, visibleWorld, minimapGeometry, minimapPointToWorld, isEditableTarget,
  };
});
