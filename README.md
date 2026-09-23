# AI Image Hub

多平台 AI 生图工作台，统一接入 LibTV、Lovart 和 OpenAI Images 兼容 API 三类执行器，
支持私有化部署交付。系统将用户原始提示词直接提交给所选模型，不做提示词扩写。

仓库自带一份静态前端演示，源码在 `docs/`（纯本地模拟数据，不调用任何付费 API，
也不连后端）。把 GitHub Pages 的发布源指向 `docs/` 目录即可在线访问；也可以直接在
本机用任意静态服务器打开 `docs/index.html`。

## 已实现

- 账号登录、管理员创建/停用账号
- 两级角色：管理员（可进管理台，配置平台与模型）与普通账号
- 一个账号可拥有多个项目，一个项目固定对应一张独立画布；项目可创建、重命名、归档，
  提示词、任务、素材和结果按项目隔离
- 项目画布/草稿状态自动保存；旧版 SQLite 历史记录自动迁入保留项目
- 普通账号不显示也不能访问系统管理
- Lovart、LibTV、OpenAI Images 兼容 API 三类执行器
- 平台账号池：同一渠道可配置多个账号，按固定顺序路由，上游明确报额度不足时切到下一个
- 模型能力驱动的平台、模型、比例和质量选择（含逐模型 API 路由与上游模型名候选）
- 参考图上限由模型能力决定（默认最多 14 张），校验格式、尺寸和文件大小
- 持久化任务队列、进程中断保护、有限的安全重试
- 每次提交不可变记录原始提示词、参数、参考图顺序和父任务关系
- 冻结每次任务的供应商模型能力快照
- CSRF 防护、登录尝试限速与生成提交幂等键
- 可选的提示词优化：调用服务器上自行部署的 skill 脚本改写提示词（默认关闭）
- 个人提示词历史搜索、平台/结果标记筛选、一键再次使用
- 成功结果的「满意 / 采用 / 不满意」三态标记
- 结果预览与下载、管理端任务和模型连接状态
- 前端不使用 CDN、外部字体、遥测或第三方脚本

## 启动

```bash
cp .env.example .env
# 必须先把 .env 里的占位值换成自己的随机值，再填写实际平台凭据
uv sync --dev
uv run uvicorn image_hub.app:app --host 0.0.0.0 --port 5190
```

打开 `http://服务器地址:5190`。首次启动根据 `.env` 创建管理员账号。

也可以使用容器启动：

```bash
./deploy.sh
```

Compose 默认把宿主机的 `data/` 与 `storage/` 挂载进容器，并把 5190 端口只绑定到
`127.0.0.1`。LibTV / Lovart 执行器直接调用官方 skill 脚本，不需要安装外部 CLI；
容器里默认不挂载 skill 目录，需要时在 `docker-compose.yml` 中取消对应只读挂载，
并让 `IMAGE_HUB_LIBTV_SKILL_SCRIPTS` / `IMAGE_HUB_LOVART_SKILL_SCRIPT` 指向容器内路径。

两个已知的部署约束：

- **容器以非 root（uid 10001）运行**。跑过之后宿主机 `data/`、`storage/` 的属主会变成
  这个 uid，普通用户再执行 `docker build` 会因无法 stat 这两个目录而失败。用 `sudo`
  执行 `deploy.sh` 即可，不要去改这两个目录的权限位 —— 改了会让容器内的进程写不进去。
- `docker compose` 若抛 `KeyError: 'ContainerConfig'`，那是 compose v1 读取新版本
  Docker 所建容器时的已知问题，`docker compose down --remove-orphans` 再 `up` 即可。

## 安全须知

**部署前必须先改掉两个占位值。** 服务启动时会强制校验下面两个配置，值缺失或仍是
`.env.example` 里的占位值（`replace-with-a-long-random-secret`、`change-me-now`）都会
直接拒绝启动，任何环境（包括本地开发）都一样：

| 配置项 | 用途 |
|---|---|
| `IMAGE_HUB_SESSION_SECRET` | Session 签名的根密钥，泄漏即等于可伪造登录态 |
| `IMAGE_HUB_BOOTSTRAP_ADMIN_PASSWORD` | 首次建库时创建的管理员密码 |

用随机值替换，例如：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

其余边界：

- 容器以非 root 用户运行。挂载 `data/` 和 `storage/` 时请确保该用户对这两个目录可写。
- `5190` 是明文 HTTP 端口，不要直接暴露到公网，应由反向代理提供 HTTPS。
- 初始管理员密码只在首次建库时使用。库里已有账号之后，改密码要走管理后台，改 `.env`
  不再生效。

## 配置执行器

三个执行器都不需要额外安装可执行程序：

| 执行器 | 依赖 | 需要的配置 |
|---|---|---|
| LibTV | 官方 `libtv-skill` 的 `scripts/` 目录 | `IMAGE_HUB_LIBTV_SKILL_SCRIPTS`、`IMAGE_HUB_LIBTV_ACCESS_KEY` |
| Lovart | 官方 `lovart-api` 的 `agent_skill.py` | `IMAGE_HUB_LOVART_SKILL_SCRIPT`、`IMAGE_HUB_LOVART_ACCESS_KEY`／`IMAGE_HUB_LOVART_SECRET_KEY` |
| API | 无 | `IMAGE_HUB_OPENAI_IMAGE_*` |

```env
IMAGE_HUB_LIBTV_SKILL_SCRIPTS=/opt/libtv-skill/scripts
IMAGE_HUB_LIBTV_ACCESS_KEY=your-access-key
IMAGE_HUB_LOVART_SKILL_SCRIPT=/opt/lovart-api/scripts/agent_skill.py
IMAGE_HUB_LOVART_ACCESS_KEY=ak_xxx
IMAGE_HUB_LOVART_SECRET_KEY=sk_xxx
```

可选微调项：`IMAGE_HUB_LIBTV_IM_BASE`、`IMAGE_HUB_LOVART_BASE_URL` 用于指向自建网关，
`IMAGE_HUB_LOVART_THREAD_MODE`（`fast` / `thinking`）与
`IMAGE_HUB_LOVART_CHAT_TIMEOUT_SECONDS` 控制 Lovart 的推理模式与单次生成等待上限。

这两组凭据与脚本路径也可以在「管理后台 → 平台与模型」里填写，管理员保存后即可用
「测试连接」验证。管理后台的取值优先于环境变量，且只以 `0600` 私有文件保存在服务端。

skill 脚本由服务进程自己的 Python 解释器执行，因此 Windows 上不需要 `python3` 别名，
也不需要把 skill 装进 `PATH`。

## 配置图像 API

API 通道由三个环境变量驱动：`IMAGE_HUB_OPENAI_IMAGE_BASE_URL`（上游地址）、
`IMAGE_HUB_OPENAI_IMAGE_API_KEY`（凭据，只存于服务端、不发送到浏览器）、
`IMAGE_HUB_OPENAI_IMAGE_MODELS`（模型清单）。模型清单留空即回落内置的 6 个模型：
`nano-banana-2-lite` / `nano-banana-2` / `nano-banana-pro` / `gpt-image-2.5` /
`gpt-image-2.5-flare` / `gpt-image-2.5-sunburst`。

这三项也能在「管理后台 → 平台与模型」里填写（后台取值优先）；**多 API 路由清单只在那里
维护**，因为它带密钥，不进环境变量。

### 模型清单格式

`IMAGE_HUB_OPENAI_IMAGE_MODELS` 用逗号分隔多个模型，每个模型的字段用竖线 `|` 分隔，
同一字段内的多个取值用分号 `;` 分隔：

```
模型ID|显示名称|比例;|分辨率;|最大参考图数|启用|质量;|附加请求头;|API 路由|模型介绍|上游模型名候选
```

各字段均可省略（省略即取默认值）：

- 比例 / 分辨率：省略 = 该模型支持的全集；
- 最大参考图数：默认 4（范围 0–14）；
- 启用：默认 `true`（`true`/`false`，或 `1`/`0`、`on`/`off`、`启用`/`停用`）；
- 质量：默认全集；允许留空表示该模型没有质量维度（如 nano-banana 系列）。

例：

```env
IMAGE_HUB_OPENAI_IMAGE_MODELS=doubao-seedream-5-0-pro-260628|Seedream 5.0 Pro,gpt-image-1|GPT Image 1
```

### 逐模型请求头（第 8 字段）

格式为 `名称:值`，多条用 `;` 分隔。它**只作用于本模型**，与其他模型互不干扰；
非空值会覆盖同名的默认头（默认头是 `Authorization: Bearer <API Key>`）。**值为空表示
删除该条头** —— 上游不需要 Bearer 鉴权时，用 `Authorization:` 即可去掉默认鉴权。

```env
IMAGE_HUB_OPENAI_IMAGE_MODELS=model-a|Model A|1:1;16:9|1K;2K|4|true|auto|X-Api-Key:own-key;X-Client:v1
```

⚠️ 请求头的值里不能包含分号（分号是字段内分隔符）。

### 逐模型 API 路由（第 9 字段）

同一个模型可以从不同的中转 API 出图。第 9 字段填**一条**路由名（人工选定，只能填一条）：
留空 = 走上面那对默认地址与密钥。管理后台不会让人手写这一字段：模型行里是一个下拉框，
直接选「默认地址（上方 Base URL）」或某条路由。

路由清单带密钥，所以**不在环境变量里**，只存服务端的 `provider-config.json`；在
「管理后台 → 平台与模型」的「API 路由」框里逐行填写，每行 6 个字段：

```
名称|地址|密钥|族|异步|启用
```

- 名称：只能含字母、数字与 `_ . -`（不能有空白或 `|` `,` `;`），最长 40 字符；清单最多 20 条；
- 密钥：**留空 = 保持该路由已保存的密钥**（保存后不回显）；首次配置该路由时留空则直接
  借用默认 API Key；
- 族：`grsai_native` 或 `openai_compat`，留空按模型名推断；
- 异步：留空按族与模型推断；**只对 `grsai_native` 合法**，OpenAI 兼容的路由填了会被拒绝；
- 启用：留空 = 启用。停用的路由仍可被模型选中（下拉里标着「已停用」），但选中它的模型
  对用户不可见。

**选路由管理员决定，系统不换路**：一个模型走哪条 API 完全看它在管理页里选的那条 ——
没有候选名单、没有轮换、没有粘性游标。选定的那条**停用**或**被标为额度耗尽**时，该模型
直接不出现在用户侧列表里（原因在管理页的路由状态表上写着），系统既不替管理员改走另一条
线，也不退回默认地址。

失败时只记账：上游原文留在「路由状态表」里；只有额度不足 / 限流 / 5xx / 连不上才额外标
「额度耗尽」，鉴权与参数错只记原文不标失效。标记**不会自动恢复**：确认充值或换套餐后，
在管理页手工点「清除额度耗尽」。产品查不到上游余额，任何"自动恢复"都是拿猜测去覆盖一个
已知的坏状态。

### 逐模型介绍（第 10 字段）

第 10 字段是**给用户看的一句话介绍**，显示在「选择平台与模型」的弹层里，接在状态小字后面。
它纯粹是展示文案，不参与任何执行逻辑。值里不能含半角逗号、竖线或分号，最长 160 字符。

⚠️ 和请求头、路由一样，模型行是**全量覆盖**的：保存时把介绍一并回显进表单，就是为了不让
你改别的字段时把它静默清空。

### 展示名与上游模型名解绑（第 11 字段）

同一个模型在不同中转里叫法不同是常态。第 11 字段让**一个展示名对多个上游模型名**：
上游名候选用分号分隔、**按优先级**；留空 = 只用左边的模型标识，所以旧配置不需要任何迁移。
执行时按顺序取第一个「路由还没被标失效」的名字。

四条纪律：

- 名字里**不能有空白**，也不能含逗号或竖线；管理页的输入框会当场拦下这两种。
- 与模型标识一样**保存后不回显** —— 改动别的字段时要一并重新填写，输入框下方写着这句提醒。
- 候选只在**同一条路由**内生效：路由（第 9 字段）仍然是单选的。
- 能力（比例 / 分辨率 / 像素换算）仍然按**模型标识**查表。

### 两条端点分支

请求形状由**路由的「族」**决定，而不是模型名：

- **`grsai_native`** 走 `{base_url}/api/generate`：传 `aspectRatio` + `imageSize` +
  `replyType`，参考图参数名是 `images`。
- **`openai_compat`** 走 `{base_url}/images/generations`：只传像素 `size`，参考图参数名是
  `image`。

## 平台账号池

LibTV / Lovart 两个平台渠道各有一个**全局账号池**，可以登记多个账号：

- 路由是**固定顺序**的：按管理页里的顺序升序，跳过停用或已被标为额度耗尽的号；
- 只有上游明确报「额度不足」时才切到下一个号，且**只换新提交**：任务一旦被上游受理
  （拿到了 task id），任何情况下都不重提 —— 重提等于为同一张图付两次钱；
- 每个 `(渠道, 账号)` 组合各自是一份执行快照，换号会把「用哪个号」一并冻结进任务；
- 凭据按账号隔离存放于 `storage/private/`，池里没有号时回落到「单账号配置」。

## 并发与容量

生图受上游延迟支配、本地几乎不吃 CPU，所以「本机能同时跑多少」和「上游能同时扛多少」
是两件事。

### 平台侧：我们的并发槽位

- `IMAGE_HUB_WORKER_CONCURRENCY`（默认 4）就是「同时执行的生成任务数」。worker 起这么多
  条线程，每条是一个独立**槽位**，一次只跑一个任务，所以槽位数 == 并发任务数。
- 因为生图是上游延迟型工作，槽位可以明显多于 CPU 核数——本地几乎不占算力，真正的上限
  在上游。不要为了"压满 CPU"去调它。
- 提交侧另有账号级上限 `IMAGE_HUB_MAX_ACTIVE_TASKS_PER_USER`（默认 5，**只算
  `queued` + `running`**），超出返回 `429`。它是「账号维度」的闸门，跟 worker 槽位无关：
  槽位决定"同时在跑几个"，这个上限决定"一个账号能挂起多少个未完成任务"。

### 上游侧：真实容量必须实测

上游容量是**时变**的：过载时返回 `The model load is too high, please try again later`，
且可能发生在提交后数十秒 —— 任务是被调度过、开始跑了才被拒，不是入口处立即限流。
⚠️ 过载时**单发也会失败**，所以别把它误判成"我们并发太高被限流"；提高
`WORKER_CONCURRENCY` 不会触发它，上游自己忙才是原因。

### 瞬时错误会退避重试

上游过载属于**瞬时**故障，执行器会退避后重提：

- `IMAGE_HUB_API_SUBMIT_RETRIES`：首次之外的重试次数（总尝试次数 = `1 + 此值`）。
- `IMAGE_HUB_API_RETRY_BACKOFF_SECONDS`：退避基数，第 `n` 次退避 = `基数 * n` 秒。

额度不足、参数非法、内容被拦截这类错误**重试没有意义**，执行器直接判失败、不重试。

### 异步模式

`IMAGE_HUB_API_REPLY_MODE` 控制 API 通道的回复方式：

- `async`（默认）：提交后立刻拿到 `task id`，再轮询 `{base}/api/result`
  （返回 `{id, status, progress, results}`，`progress` 可做进度展示）直到终态。
- `sync`：一次请求阻塞到出图（十几秒起步，过载时更久）。

⚠️ **异步本身不提高我们的并发任务数。** 并发数由 `IMAGE_HUB_WORKER_CONCURRENCY`
（槽位数）决定；异步省下的是「一个槽位被一次长请求押住十几秒」+「过载时一次廉价的
重新提交」。

⚠️ 异步**不重试轮询超时**：轮询超过 `IMAGE_HUB_API_POLL_TIMEOUT_SECONDS` 仍未拿到终态
时，上游那个任务已经创建、可能已在计费，重提等于为同一张图付两次钱。此时执行器会把
task id 记进任务行，任务落到待恢复（`recovery_required`）交人工收敛，**既不盲目重跑**。

## 提示词优化（可选）

默认关闭。开启后服务端会调用**自行部署的 skill 脚本**改写提示词：脚本路径、参数模板、
LLM API Base URL / Key / 模型都在「管理后台 → 提示词优化」里配置，配置落在
`storage/prompt-optimize-config.json`（0600）。

两种模式：`optimize`（纯文本改写）与 `agent`（额外把参考图交给支持视觉的模型做诊断后
改写）。脚本契约：命令行参数由模板渲染（支持 `{prompt_file}` / `{prompt}` / `{model}`），
凭据经环境变量传入，结果从 stdout 的最后一段 JSON 里取。

## 数据目录

- SQLite：`data/image-hub.db`
- 参考图与结果：`storage/generations/<generation-id>/`
- 私有凭据与配置：`storage/private/`

正式多人环境建议将数据库切换为 PostgreSQL，并把 `storage` 挂载到持久卷或内部对象存储。

## 验证

```bash
uv run ruff check src tests
uv run pytest -q
```

## 隐私边界

浏览器只访问部署本系统的同源服务。只有后端执行器会按管理员配置访问 Lovart、LibTV 或
图像 API 上游；页面没有 CDN、外部字体、图标服务、分析脚本或遥测。提示词和图片属于业务
机密数据，应通过内网、HTTPS、服务器访问控制和定期备份保护。
