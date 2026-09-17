---
version: alpha
name: XGM AI Image Hub Precision Canvas
description: Figma 的画布克制、Vercel 的组件精度与 Apple 的图片优先，共同构成面向部门用户的轻量 AI 生图工具。
colors:
  primary: "#171717"
  canvas: "#F7F7F5"
  surface: "#FFFFFF"
  surfaceSubtle: "#FAFAF9"
  textPrimary: "#171717"
  textSecondary: "#666666"
  textTertiary: "#8A8A84"
  border: "rgba(0, 0, 0, 0.08)"
  borderStrong: "rgba(0, 0, 0, 0.14)"
  interactive: "#171717"
  interactiveHover: "#000000"
  focus: "#2563EB"
  success: "#276F50"
  successSurface: "#EAF5EF"
  danger: "#B33A32"
  dangerSurface: "#FBEDEC"
  disabled: "#B6B6B0"
typography:
  heading-lg:
    fontFamily: "Inter, PingFang SC, Microsoft YaHei, system-ui, sans-serif"
    fontSize: 1.5rem
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  heading-md:
    fontFamily: "Inter, PingFang SC, Microsoft YaHei, system-ui, sans-serif"
    fontSize: 1rem
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "-0.012em"
  body:
    fontFamily: "Inter, PingFang SC, Microsoft YaHei, system-ui, sans-serif"
    fontSize: 0.875rem
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "-0.005em"
  ui:
    fontFamily: "Inter, PingFang SC, Microsoft YaHei, system-ui, sans-serif"
    fontSize: 0.8125rem
    fontWeight: 500
    lineHeight: 1.35
  caption:
    fontFamily: "Inter, PingFang SC, Microsoft YaHei, system-ui, sans-serif"
    fontSize: 0.75rem
    fontWeight: 400
    lineHeight: 1.4
rounded:
  xs: 4px
  sm: 6px
  md: 8px
  lg: 10px
  pill: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  xxl: 32px
components:
  button-primary:
    backgroundColor: "{colors.interactive}"
    textColor: "{colors.surface}"
    typography: "{typography.ui}"
    rounded: "{rounded.sm}"
    padding: 10px
    height: 36px
  button-primary-hover:
    backgroundColor: "{colors.interactiveHover}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.textPrimary}"
    typography: "{typography.ui}"
    rounded: "{rounded.sm}"
    padding: 8px
    height: 32px
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.textPrimary}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: 10px
    height: 36px
  node-request:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.textPrimary}"
    rounded: "{rounded.md}"
    padding: 16px
    width: 344px
  node-image:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.textPrimary}"
    rounded: "{rounded.md}"
    padding: 4px
  status-success:
    backgroundColor: "{colors.successSurface}"
    textColor: "{colors.success}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    padding: 6px
---

## Overview

XGM AI Image Hub 是部门内部使用的多平台 AI 生图聚合工具。界面不承担营销、教学或 Agent 对话；视觉必须让图片成为第一主角，让生图请求成为轻量工具，让节点连线只表达参考输入和结果派生。

设计融合三种成熟语言：

- Figma：黑白工具界面、内容提供颜色、画布与浮动工具条。
- Vercel：精密灰阶、6–8px 小圆角、shadow-as-border、明确的控件状态。
- Apple：界面主动后退、系统字体、图片优先、充足但不浪费的留白。

禁止照搬三者的营销页面。这里是桌面创作工具，不是官网。

## Colors

界面 chrome 坚持中性黑白灰。生成图片本身是页面最主要的颜色来源。

- 画布使用 `#F7F7F5`，点阵必须极淡，缩放和平移时不抢视觉。
- 所有节点使用白色表面；通过 `0 0 0 1px rgba(0,0,0,.08)` 的 shadow-as-border 建立边界。
- 主操作使用近黑色，不使用 AI 紫色、青色渐变或装饰性色块。
- 绿色仅表示成功、在线和已保存；红色仅表示错误和危险。
- 蓝色只用于键盘焦点环，不作为品牌装饰色。

## Typography

字体只使用本地优先栈，不加载 CDN：`Inter, PingFang SC, Microsoft YaHei, system-ui, sans-serif`。

层级依靠字号、字重和留白，不使用大面积全大写标签。标题保持紧凑负字距；正文不低于 12px。平台、模型、比例与状态等元数据使用 12–13px，但必须保持可读对比度。

## Layout

顶部栏高度控制在 48–52px。项目名是当前上下文，保存状态是弱状态，不得与主要动作竞争。历史、管理和退出属于次级导航。

画布工具条采用单个浮动容器，放置上传、新建生图、适应画布和缩放；同一组图标保持 1.5px 线宽。上传和新建生图可以保留文字，缩放与适应使用 SVG 图标和 tooltip。

图片节点应让图片占据至少 90% 的可视面积，文件名、状态和动作进入 28–32px 的薄底栏。生图节点宽 328–352px，控件遵循 8px 节奏。结果节点不使用大块状态底色。

## Elevation & Depth

不使用厚重卡片阴影。层级仅分三级：

1. 画布：无阴影。
2. 普通节点：`0 0 0 1px rgba(0,0,0,.08), 0 2px 4px rgba(0,0,0,.04)`。
3. 聚焦或拖拽节点：边界增强并增加 `0 8px 24px rgba(0,0,0,.08)`。

浮动菜单和下拉层可以使用更明显的阴影，但不超过 12px 圆角。

## Shapes

矩形组件使用 6–10px 圆角，避免所有元素都变成胶囊。胶囊只用于状态、计数和短标签。图像本身使用 6–8px 圆角。连接端口是 10–12px 圆形，默认弱化，节点 hover、focus-within 或连线时增强。

## Components

### Top bar

- 48–52px 高，白色或 96% 不透明白色。
- 使用一条极淡底部分隔线，不使用厚阴影。
- 顶部操作统一为 32px 高；只有真正主要的动作使用深色填充。

### Canvas toolbar

- 单个白色浮动容器，8px 圆角，shadow-as-border。
- 分组之间使用细分隔线，不把每个按钮做成独立大胶囊。
- hover 只改变浅灰背景；active 使用近黑文字和明确按下反馈。

### Image and result nodes

- 图片是主视觉，不使用厚标题栏。
- 顶部只允许非常轻的状态标识；常用动作集中到底部薄栏。
- 评价控件在 hover 或 focus-within 时出现，使用固定高度避免布局跳动。
- 打开、下载、评价和连接端口不得覆盖图片主体。

### Generation request node

- 像精密的浮动工具面板，不像后台表单。
- Prompt 占主要面积；组合下拉与数量控件紧凑排列。
- 标签和输入间距统一，禁用态同时使用 disabled 属性、降低强调和解释文字。
- 生成按钮是节点内唯一高强调控件。

### History page

- 图片优先，卡片图片占比约 70–78%。
- 筛选区使用单一工具条表面；字段名称与当前值清晰，不堆叠大标签。
- 每张卡片默认只显示一项主操作；其他操作降低视觉权重。
- 不增加成本、标签、审图面板等 V1 范围外信息。

### Empty, processing, error, unavailable

- 空画布只显示一句简短操作提示，不做营销 hero。
- 处理中使用小状态点、细进度条和短文案，不使用大色块。
- 错误和模型不可用状态必须说明下一步：重新选择模型、重新上传图片或联系管理员。
- “重新选择”必须明确写成“重新选择图片”，避免与重试混淆。

## Do's and Don'ts

### Do

- 让图片本身提供颜色，UI chrome 保持中性。
- 使用 8px 网格统一间距。
- 使用 shadow-as-border 获得精密边界。
- 为 hover、active、focus-visible、disabled、dragging、drop-target 定义完整状态。
- 所有状态过渡控制在 120–180ms，支持 `prefers-reduced-motion`。
- 保证拖拽之外仍有按钮/键盘路径；图标按钮有 `aria-label` 和 tooltip。
- 在 1440px、1024px、768px、375px 下检查无横向溢出和遮挡。

### Don't

- 不使用 AI 紫、青色渐变、玻璃拟态和霓虹光。
- 不增加左侧大导航、模板市场、新手教程或审图器。
- 不把图片节点和生图节点做成同等厚重的卡片。
- 不使用超过 12px 的矩形大圆角，不滥用胶囊。
- 不用 emoji 作为结构图标。
- 不通过缩放或位移动画导致控件和节点抖动。
- 不更改已通过测试的节点连接、生成、历史、权限和 API 行为。
