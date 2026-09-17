# XGM AI Image Hub

公司部门内部使用的多平台 AI 生图工作台。把「写提示词 → 选平台/模型/比例/分辨率 →
挂参考图 → 出图 → 筛选与复用」收敛到一张节点画布上，统一接入 **Lovart**、**LibTV**
与任意 **OpenAI Images 兼容 API**。

系统把用户的原始提示词**原样**提交给所选模型，不做提示词扩写——模型看到的就是你写的那句话。

## 功能

**画布与工作流**

- 一个账号可有多个项目，一个项目固定对应一张独立画布
- 节点连线表达参考输入与结果派生关系；画布与草稿状态自动保存
- 最多 14 张参考图，逐张校验格式、尺寸与文件大小
- 结果卡片支持预览与下载，成功结果可标记「满意 / 采用 / 不满意」
- 提示词历史支持全文搜索、按平台与结果标记筛选、一键再次使用
- 支持外部图片直接拖入与粘贴

**执行器**

- 三类执行器：Lovart、LibTV、OpenAI Images 兼容 API，均由模型能力表驱动
- **不依赖任何外部 CLI**：直接调用官方 skill 的 Python 脚本
- 比例、分辨率、质量由所选模型的能力快照决定，前端按实际可用档位收窄选项
- 管理后台可「测试连接」：LibTV 建一次空会话、Lovart 只查生成模式，**都不产生生成费用**

**任务可靠性**

- 持久化任务队列，基于租约（lease）而非出队，进程中断不丢任务
- 每次提交不可变记录原始提示词、参数、参考图顺序与父任务关系
- 冻结每次任务的供应商模型能力快照，上游改配置不影响在跑的任务
- 提交幂等键，重复点击不会重复下单
- 租约过期的任务**不会自动重跑**，而是转入「恢复队列」由管理员决策
  （重新查询上游或收敛为失败）——因为中断的那次可能已经在上游开始计费

**账号与隔离**

- 账号登录、管理员创建/停用账号、项目与历史按用户隔离
- 普通账号看不到也访问不到系统管理
- CSRF 双层防护、登录尝试限速、密码修改后旧会话自动失效

**前端**

- 零外部依赖：不使用 CDN、外部字体、图标服务、分析脚本或遥测
- 画布核心（`canvas-core.js`）是独立 UMD 模块，可脱离浏览器单测

## 技术栈

| 层 | 选型 |
|---|---|
| Web | FastAPI + Jinja2 服务端渲染 |
| 数据 | SQLAlchemy 2.0 + SQLite（可换 PostgreSQL） |
| HTTP | httpx |
| 前端 | 原生 JS + 单张 SVG 画布，无构建步骤 |
| Python | >= 3.11 |

## 快速开始

```bash
cp .env.example .env
# 必改：IMAGE_HUB_SESSION_SECRET、IMAGE_HUB_BOOTSTRAP_ADMIN_PASSWORD
# 并填写至少一个平台的凭据
uv sync --dev
uv run uvicorn image_hub.app:app --host 0.0.0.0 --port 5190
```

打开 `http://<服务器地址>:5190`。首次启动会按 `.env` 创建管理员账号；该密码**只在首次建库时
生效**，之后改密码请走「账号设置」。

容器启动：

```bash
docker compose up -d --build
```

Compose 默认把两个 skill 目录只读挂进容器。容器端口只绑定 `127.0.0.1`，应由内网反向代理
提供 HTTPS——**不要**把 5190 直接暴露到公网。

## 配置执行器

三个执行器都不需要额外安装可执行程序：

| 执行器 | 依赖 | 需要的配置 |
|---|---|---|
| LibTV | 官方 `libtv-skill` 的 `scripts/` 目录 | `IMAGE_HUB_LIBTV_SKILL_SCRIPTS`、`IMAGE_HUB_LIBTV_ACCESS_KEY` |
| Lovart | 官方 `lovart-api` 的 `agent_skill.py` | `IMAGE_HUB_LOVART_SKILL_SCRIPT`、`IMAGE_HUB_LOVART_ACCESS_KEY`／`SECRET_KEY` |
| API | 无 | `IMAGE_HUB_OPENAI_IMAGE_*` |

```env
IMAGE_HUB_LIBTV_SKILL_SCRIPTS=/opt/libtv-skill/scripts
IMAGE_HUB_LIBTV_ACCESS_KEY=your-access-key
IMAGE_HUB_LOVART_SKILL_SCRIPT=/opt/lovart-api/scripts/agent_skill.py
IMAGE_HUB_LOVART_ACCESS_KEY=ak_xxx
IMAGE_HUB_LOVART_SECRET_KEY=sk_xxx
```

这些值也可以直接在「管理后台 → 平台与模型」里填写——**管理后台的取值优先于环境变量**，
且只以私有文件保存在服务端，不会下发到浏览器。

skill 脚本由服务进程自己的 Python 解释器执行，所以 Windows 上不需要 `python3` 别名，
也不需要把 skill 装进 `PATH`。

### OpenAI Images 兼容 API

`IMAGE_HUB_OPENAI_IMAGE_MODELS` 以逗号分隔，每项格式为 `模型ID|显示名称`：

```env
IMAGE_HUB_OPENAI_IMAGE_MODELS=doubao-seedream-5-0-pro-260628|Seedream 5.0 Pro,gpt-image-1|GPT Image 1
```

上游需兼容 `POST /images/generations`。如果上游在内网地址，可通过
`IMAGE_HUB_API_PRIVATE_NETWORK_ALLOWLIST` 显式放行网段——默认只允许公网 HTTPS 端点，
并带 DNS rebinding 校验。

## 平台与模型行为

- **Lovart** 上架 22 个 IMAGE 模型。其 `chat` 子命令没有任何尺寸参数，因此比例与分辨率
  会**随提示词一并下发**；每个比例支持的档位不同（有的比例没有 4K），前端在切换比例时会
  自动把分辨率收敛到该比例真实支持的档位。
- **LibTV** 是单入口「智能编排」——上游不允许用户侧指定模型或拆解任务，因此不逐个枚举模型。
- **API** 模型由 `IMAGE_HUB_OPENAI_IMAGE_MODELS` 定义，参考图上限可在管理后台按模型设置为
  0–14。

## 数据目录

- SQLite：`data/image-hub.db`
- 参考图与结果：`storage/generations/<generation-id>/`
- 平台凭据：`storage/private/`（服务端私有文件，不进版本库）

正式多人环境建议把数据库切到 PostgreSQL，并把 `storage` 挂到持久卷或内部对象存储。

## 开发与验证

```bash
uv run ruff check src tests
uv run pytest -q
```

`tests/test_canvas_core.py` 需要本机有 `node`（它直接用 `node -e` 跑画布核心的断言）。

## 安全与隐私边界

- 浏览器只访问本系统的同源服务；页面没有 CDN、外部字体、图标服务或遥测
- 只有后端执行器会按管理员的配置访问 Lovart、LibTV 或图像 API 上游
- 凭据只保存在服务端，不下发到浏览器；错误信息会剥掉密钥、URL 与 token
- 安全响应头齐全（CSP / nosniff / DENY / Referrer-Policy / Permissions-Policy）
- 提示词与生成图片属于内部数据，请通过内网、HTTPS、服务器访问控制与定期备份保护

## 许可

本仓库目前用于公司内部部署，**未附带开源许可文件**。如需对外分发，请先补充 `LICENSE`。
