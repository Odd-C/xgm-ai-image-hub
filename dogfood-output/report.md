# XGM AI Image Hub V1 精度画布 UI 最终浏览器验收报告

## 执行摘要

- 最终结论：PASS / SHIP。
- 自动化浏览器场景：2/2 通过；正式站与静态 Demo 均使用 Playwright Chromium 实测。
- 最终未解决的发布阻塞问题：0（Critical 0 / High 0 / Medium 0）。
- 付费 Provider 调用：0。正式站 Worker 明确关闭，模型不可用态通过浏览器内本地状态构造；Demo 仅执行本地虚构生成。
- 对比结论：顶部 chrome、浮动工具条、请求面板、结果节点和 History 均从工程原型视觉收敛到克制中性的桌面创作工具。

## 测试环境

- 正式站：`127.0.0.1:5191`，独立临时 SQLite 与 storage，虚构管理员账号，`IMAGE_HUB_WORKER_ENABLED=false`。
- 静态 Demo：`127.0.0.1:5192`，所有图片和交互均为同源本地虚构数据。
- 浏览器：Playwright Chromium 153，无头模式。
- 视口：1440 × 960、1024 × 900、768 × 900、375 × 812。
- 网络与控制台：页面 JavaScript error 0；非同源资源请求 0；四个目标宽度文档横向溢出 0。

## 视觉与交互验收

1. 顶栏实测高度 50px；项目名保持上下文主次，保存、历史、管理、退出降为次级控制：通过。
2. 上传、新建生图、适应画布与缩放合并为单个 8px 浮动工具条；导航与缩放采用 1.5px SVG 图标、可访问名称和 tooltip：通过。
3. 成功图片/结果节点取消厚标题栏，图片上方不覆盖控制，动作与元数据进入 30px 底栏：通过。
4. 生图节点宽度 350px，Prompt 为主要输入，组合选择器与数量按 8px 节奏压缩，Generate 为唯一高强调操作：通过。
5. 画布 `#F7F7F5`、白色表面、`#171717` 主文字、shadow-as-border、中性色 chrome：通过。
6. hover、focus-visible、disabled、dragging、drop-target、connecting、processing、success、unavailable/reselect 均有无布局位移状态：通过。
7. “原模型已不可用”选项提供“请选择可用模型”的下一步；错误说明包含选择已启用模型/联系管理员；恢复按钮明确为“重新选择图片”：通过。
8. 连接端口默认弱化，节点 hover/focus 与连接状态增强；连线降低对比度：通过。
9. History 图片区域维持约 75% 视觉占比，筛选合并为单一精密工具条，元数据单行，继续使用为唯一主操作：通过。
10. Demo 生成过程实测 queued → running → succeeded；4 个虚构结果完成，无真实 Provider 调用：通过。
11. 结果评价控件在 hover 和键盘 focus 后可见；移动端保持可操作：通过。
12. 1440、1024、768、375px 工具条不超出视口，页面无横向 document overflow：通过。

## 最终证据

- 正式站画布（1440）：`dogfood-output/screenshots/formal-workspace.png`
- 正式站画布（375）：`dogfood-output/screenshots/formal-workspace-375.png`
- Demo 处理中：`dogfood-output/screenshots/demo-processing.png`
- Demo History（1440）：`dogfood-output/screenshots/demo-history.png`
- Demo History（375）：`dogfood-output/screenshots/demo-history-375.png`
- 改造前正式站：`dogfood-output/screenshots/before-formal-workspace.png`
- 改造前 Demo History：`dogfood-output/screenshots/before-demo-history.png`

## 自动化回归

- `ruff check src tests`：通过。
- `pytest -q`：47 passed，3 条上游 deprecation warning。
- `python3 -m compileall -q src tests`：通过。
- `node --check`：`docs/demo.js`、`canvas-core.js`、`app.js`、`history.js` 全部通过。
- `git diff --check`：通过。
- `@google/design.md lint DESIGN.md`：0 errors；contrast warnings 0。仅有 10 条未被组件层直接引用的 token warning。

## 测试说明与限制

真实 Provider 执行属于本轮明确禁止范围，因此未测试真实上游生成质量、计费、配额或供应商可用性。正式站浏览器验收只访问本地同源应用并保持 Worker 关闭；Demo 的处理与成功结果完全由本地虚构逻辑产生。所有提交证据均不包含凭据、内部地址、真实提示词或真实上游任务数据。
