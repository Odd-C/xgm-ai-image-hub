(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const layout = $("#admin-layout");
  const menuButton = $("#admin-menu-button");
  const scrim = $("#admin-scrim");

  function setDrawer(open) {
    if (!layout || !menuButton || !scrim) return;
    layout.classList.toggle("drawer-open", open);
    menuButton.setAttribute("aria-expanded", String(open));
    scrim.hidden = !open;
  }

  menuButton?.addEventListener("click", () => setDrawer(!layout.classList.contains("drawer-open")));
  scrim?.addEventListener("click", () => setDrawer(false));
  window.addEventListener("resize", () => {
    if (!matchMedia("(max-width: 768px)").matches) setDrawer(false);
  });

  function closeDialog(dialog) {
    dialog.close();
    dialog.querySelector("form")?.reset();
  }

  $$('[data-open-dialog]').forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.openDialog);
      if (dialog instanceof HTMLDialogElement) dialog.showModal();
      button.closest("details")?.removeAttribute("open");
    });
  });
  $$('dialog [data-close-dialog]').forEach((button) => {
    button.addEventListener("click", () => closeDialog(button.closest("dialog")));
  });
  $$("dialog.admin-dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) closeDialog(dialog);
    });
  });

  $$('[data-page-size]').forEach((select) => {
    select.addEventListener("change", () => {
      const url = new URL(window.location.href);
      url.searchParams.set("per_page", select.value);
      url.searchParams.set("page", "1");
      window.location.assign(url);
    });
  });

  const modelRows = $("#model-rows");
  const modelTemplate = $("#model-row-template");
  const providerForm = $("#provider-form");

  function addModelRow() {
    if (!modelRows || !modelTemplate) return;
    modelRows.append(modelTemplate.content.cloneNode(true));
    syncRouteOptions();
    const row = $(".model-row:last-child", modelRows);
    if (row) decorateModelRow(row, false);
    renumberModelRows();
    $(".model-row:last-child input", modelRows)?.focus();
  }

  $("#add-model-row")?.addEventListener("click", addModelRow);

  // 路由清单还没保存时，下拉里也要能选到刚写下的名字：从清单文本里取每行的名称，
  // 增量补进每个「使用的 API」下拉。只追加、不删除 —— 某个模型当前选中的名字如果在
  // 这次编辑里被删掉，静默改掉它就是人工选路要避免的那类意外；保存时服务端会拒绝
  // 「指定了未定义的路由」并明确指出是哪一行，把关交给它。
  const routeList = $('textarea[name="api_routes"]');

  function syncRouteOptions() {
    if (!routeList) return;
    const names = routeList.value
      .split("\n")
      .map((line) => line.split("|")[0].trim())
      .filter((name) => name);
    $$('select[data-field="routes"]').forEach((select) => {
      const known = new Set($$("option", select).map((option) => option.value));
      names.forEach((name) => {
        if (known.has(name)) return;
        const option = document.createElement("option");
        option.value = name;
        option.textContent = `${name} · 清单还未保存`;
        select.append(option);
      });
    });
  }

  routeList?.addEventListener("input", syncRouteOptions);
  modelRows?.addEventListener("click", (event) => {
    const remove = event.target.closest(".remove-model-row");
    if (!remove) return;
    remove.closest(".model-row").remove();
    if (!$(".model-row", modelRows)) addModelRow();
  });

  providerForm?.addEventListener("submit", (event) => {
    const rows = $$(".model-row", modelRows);
    if (!rows.length) {
      event.preventDefault();
      addModelRow();
      return;
    }
    const serialized = [];
    let invalid = null;
    rows.forEach((row) => {
      $$('input', row).forEach((input) => input.setCustomValidity(""));
      const modelId = $('[data-field="model-id"]', row);
      const label = $('[data-field="label"]', row);
      const ratios = $$('[data-field="ratio"]:checked', row).map((item) => item.value);
      const resolutions = $$('[data-field="resolution"]:checked', row).map((item) => item.value);
      const qualities = $$('[data-field="quality"]:checked', row).map((item) => item.value);
      const headersInput = $('[data-field="headers"]', row);
      const routesInput = $('[data-field="routes"]', row);
      const noteInput = $('[data-field="note"]', row);
      const upstreamsInput = $('[data-field="upstreams"]', row);
      [modelId, label].forEach((input) => input.setCustomValidity(""));
      if (headersInput && /[,|]/.test(headersInput.value)) {
        headersInput.setCustomValidity("附加请求头不能包含逗号或竖线（会截断配置）");
        invalid ||= headersInput;
      }
      // 「使用的 API」是下拉，值只能来自选项，不需要在这里校验字符。
      if (/[\s,|;]/.test(modelId.value)) {
        modelId.setCustomValidity("服务端模型 ID 不能包含空白、逗号、竖线或分号");
        invalid ||= modelId;
      }
      if (/[,|;]/.test(label.value)) {
        label.setCustomValidity("显示名称不能包含逗号、竖线或分号");
        invalid ||= label;
      }
      if (noteInput && /[,|;]/.test(noteInput.value)) {
        noteInput.setCustomValidity("模型介绍不能包含逗号、竖线或分号");
        invalid ||= noteInput;
      }
      /* 上游模型 ID 列表：分号是分隔符（留空 = 只用上面那个模型标识，也就是旧配置的
         行为），但逗号与竖线会把整条配置截断，空白则会让发出去的名字和这里写的不是一个。 */
      if (upstreamsInput && /[,|\s]/.test(upstreamsInput.value)) {
        upstreamsInput.setCustomValidity("上游模型 ID 不能包含逗号、竖线或空白（多个用分号分隔）");
        invalid ||= upstreamsInput;
      }
      if (!ratios.length || !resolutions.length) {
        const target = !ratios.length ? $('[data-field="ratio"]', row) : $('[data-field="resolution"]', row);
        target.setCustomValidity(!ratios.length ? "至少选择一个比例" : "至少选择一个分辨率");
        invalid ||= target;
      }
      serialized.push([
        modelId.value.trim(),
        label.value.trim(),
        ratios.join(";"),
        resolutions.join(";"),
        $('[data-field="max-references"]', row).value,
        $('[data-field="enabled"]', row).checked ? "true" : "false",
        qualities.join(";"),
        headersInput ? headersInput.value.trim() : "",
        routesInput ? routesInput.value.trim() : "",
        noteInput ? noteInput.value.trim() : "",
        /* 第 11 段：这个模型在各家上游分别叫什么（留空 = 只用上面那个模型标识）。
           顺序必须与 providers._parse_api_models 的字段顺序一致。 */
        upstreamsInput ? upstreamsInput.value.trim() : "",
      ].join("|"));
    });
    if (invalid) {
      event.preventDefault();
      /* 出错的那个控件很可能正因为「收起」而 display:none —— 那种状态下 reportValidity()
         弹不出任何提示，浏览器还会以 "not focusable" 静默阻断提交，人就直接卡住了。
         先把这一行展开，再报告。 */
      const broken = invalid.closest(".model-row");
      if (broken) setModelRowCollapsed(broken, false);
      invalid.reportValidity();
      invalid.focus();
      return;
    }
    $("#serialized-models").value = serialized.join(",");
  });

  /* ── API 模型行：展开 / 收起 + 拖动排序 ────────────────────────────────
     两件事都只属于管理页，所以都挂在这一段里，不往 app.js 串。
     收起态：只留「显示名称 + 服务端模型 ID」，其余字段折起来 —— 六个模型行整份
     摊开约 1400px 高，折起来才能一屏看全清单。
     排列顺序：模型在「平台 · 模型」选择器里的先后，就是这里从上到下的顺序。 */

  const MODEL_ROW_LABEL = "API 模型";

  /* 收起时会被藏起来、但又带 required 的字段。display:none 的必填控件拿不到焦点，
     浏览器会静默阻断整张表单的提交（"An invalid form control ... is not focusable"），
     所以收起期间摘掉 required，展开时原样还回。它永远有值（模板给了默认 4），
     摘掉也放不过空值。 */
  const COLLAPSED_REQUIRED_FIELDS = ['[data-field="max-references"]'];

  let modelOrderStatus = null;
  let draggedModelRow = null;
  let modelOrderBeforeDrag = "";

  function modelRowLegendText(row) {
    const legend = $("legend", row);
    if (!legend) return null;
    return (
      [...legend.childNodes].find(
        (node) => node.nodeType === Node.TEXT_NODE && node.textContent.trim()
      ) || null
    );
  }

  /* 已经落盘的行按序号排队，只有它们参与换序：新加的行还没进配置，服务端不认识。 */
  function currentModelOrder() {
    if (!modelRows) return [];
    return $$(".model-row", modelRows)
      .map((row) => row.dataset.modelIndex)
      .filter((value) => value !== undefined && value !== "");
  }

  /* 行号跟着位置走：换序后重排一遍，免得 legend 还写着旧编号。 */
  function renumberModelRows() {
    $$(".model-row", modelRows).forEach((row, index) => {
      const text = modelRowLegendText(row);
      if (text) text.textContent = `${MODEL_ROW_LABEL} ${index + 1}`;
    });
  }

  function setModelRowCollapsed(row, collapsed) {
    row.classList.toggle("collapsed", collapsed);
    const toggle = $(".toggle-model-row", row);
    if (toggle) {
      toggle.textContent = collapsed ? "展开" : "收起";
      toggle.setAttribute("aria-expanded", String(!collapsed));
    }
    COLLAPSED_REQUIRED_FIELDS.forEach((selector) => {
      const input = $(selector, row);
      if (input) input.required = !collapsed;
    });
  }

  function decorateModelRow(row, collapsed = true) {
    const legend = $("legend", row);
    if (legend && !$(".drag-model-row", row)) {
      const handle = document.createElement("button");
      handle.type = "button";
      handle.className = "drag-model-row";
      handle.title = "按住拖动，调整模型先后顺序";
      handle.setAttribute("aria-label", "拖动调整模型顺序");
      handle.textContent = "⠿";
      legend.prepend(handle);
    }
    if (legend && !$(".toggle-model-row", row)) {
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "toggle-model-row";
      toggle.textContent = "展开";
      toggle.setAttribute("aria-expanded", "false");
      legend.append(toggle);
    }
    /* 默认收起：六行整份摊开约 1400px 高，一屏装不下；收起后每行只剩「显示名称 +
       服务端模型 ID」两格 —— 正好是提交时必须填的那两格。刚加出来的那行例外，
       见 addModelRow（它要展开着等人填）。 */
    setModelRowCollapsed(row, collapsed);
  }

  function setModelOrderStatus(text, failed = false) {
    if (!modelRows) return;
    if (!modelOrderStatus) {
      modelOrderStatus = document.createElement("p");
      modelOrderStatus.className = "model-order-status";
      modelOrderStatus.setAttribute("role", "status");
      modelRows.before(modelOrderStatus);
    }
    modelOrderStatus.textContent = text;
    modelOrderStatus.classList.toggle("failed", failed);
  }

  /* 换序单独提交：只发位置序列（"2,0,1"），不发模型 ID —— 管理页刻意不回显服务端
     模型 ID，若走「保存 API 配置」那张表单，只想调个先后顺序就得把每个模型名重打
     一遍。服务端按序列重排已落盘的那几行，内容逐字节不变。 */
  async function saveModelOrder() {
    const indices = currentModelOrder();
    if (indices.length < 2) return;
    const csrf = $('input[name="csrf"]', providerForm || document)?.value || "";
    try {
      const response = await fetch("/admin/providers/api/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ csrf, order: indices.join(",") }),
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(String(response.status));
      setModelOrderStatus("顺序已保存");
    } catch (error) {
      setModelOrderStatus("顺序没能保存，刷新页面后重试", true);
    }
  }

  function finishModelRowDrag() {
    if (!draggedModelRow) return;
    const row = draggedModelRow;
    row.classList.remove("dragging");
    row.draggable = false;
    draggedModelRow = null;
    /* 位置没变就不发请求：按一下手柄又松手不该产生一次写入。 */
    if (currentModelOrder().join(",") !== modelOrderBeforeDrag) saveModelOrder();
  }

  if (modelRows) {
    /* 注意别写成 forEach(decorateModelRow)：那会把下标当第二个参数传进去（0 是假值），
       第一行就展开、其余收起，出现莫名其妙的「首行不一样」。 */
    $$(".model-row", modelRows).forEach((row) => decorateModelRow(row));
    renumberModelRows();
  }

  modelRows?.addEventListener("click", (event) => {
    const toggle = event.target.closest(".toggle-model-row");
    if (!toggle) return;
    const row = toggle.closest(".model-row");
    if (row) setModelRowCollapsed(row, !row.classList.contains("collapsed"));
  });

  /* HTML5 拖拽换序。只在按到手柄上时把这一行设成 draggable —— 整张 fieldset 常驻
     draggable 会把输入框里的选字、下拉展开一起吞成拖拽。 */
  modelRows?.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest(".drag-model-row");
    const row = handle && handle.closest(".model-row");
    if (!row) return;
    draggedModelRow = row;
    modelOrderBeforeDrag = currentModelOrder().join(",");
    row.draggable = true;
  });

  modelRows?.addEventListener("dragstart", (event) => {
    if (!draggedModelRow || event.target !== draggedModelRow) {
      event.preventDefault();
      return;
    }
    event.dataTransfer.effectAllowed = "move";
    /* Firefox 不设数据就不肯启动拖拽。 */
    event.dataTransfer.setData("text/plain", "");
    draggedModelRow.classList.add("dragging");
  });

  modelRows?.addEventListener("dragover", (event) => {
    if (!draggedModelRow) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const target = event.target.closest(".model-row");
    if (!target || target === draggedModelRow) return;
    /* 越过目标行高度的一半就换位：跟列表拖拽的直觉一致。 */
    const rect = target.getBoundingClientRect();
    const after = event.clientY > rect.top + rect.height / 2;
    modelRows.insertBefore(draggedModelRow, after ? target.nextElementSibling : target);
    renumberModelRows();
  });

  modelRows?.addEventListener("dragend", finishModelRowDrag);
  /* 手柄按下去但没真的拖动：把 draggable 收回，免得挡住后续的选字与下拉。 */
  window.addEventListener("pointerup", () => {
    if (draggedModelRow && !draggedModelRow.classList.contains("dragging")) {
      finishModelRowDrag();
    }
  });

  if (modelRows && !$(".model-row", modelRows)) addModelRow();
})();
