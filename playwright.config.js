// 前端 E2E 保护层。
//
// 为什么需要它：`static/app.js`（约 130KB）与 `canvas-core.js` 是浏览器代码，
// pytest / ruff 完全覆盖不到；`tests/canvas-refinement.spec.js` 曾经就是一份
// 「没接进任何 runner 的孤儿文件」。这份配置把那批用例接上真实 runner，
// 让画布节点层的改动有可执行的回归保护，而不是只靠 `node --check`。
//
// 只跑 `formal-*.spec.js`：`canvas-refinement.spec.js` 打的是已不存在的 demo 应用
// （选择器是 `#viewport` / `#world` / `image-hub-demo-v5`），留作历史记录不接入。
//
// 用法：
//   E2E_PYTHON=<项目 venv 的 python>  npx playwright test
// 解释器与端口都从环境变量取，配置里刻意不写死任何本机路径。
const fs = require('node:fs');
const path = require('node:path');
const {randomBytes} = require('node:crypto');

const {defineConfig} = require('@playwright/test');

const PORT = Number(process.env.E2E_PORT || 5190);
const BASE_URL = `http://127.0.0.1:${PORT}`;
const PYTHON = process.env.E2E_PYTHON || 'python';

// 会话密钥每次运行现生成：只要 ≥32 字符（config.ensure_directories() 会断言），
// 且同一轮里 webServer 与用例拿到的是同一个值。**不落字面量** —— 仓库里出现
// 任何形如 SECRET = "<长串>" 的常量都会触发凭据扫描，而它其实是个无意义的占位。
const SESSION_SECRET = randomBytes(32).toString('hex');

// 独立的运行目录：E2E 绝不碰 data/ 与 storage/ 里的真实数据。
const RUNTIME = path.join(__dirname, 'tests', '.runtime', 'e2e');
fs.mkdirSync(RUNTIME, {recursive: true});

// sqlite:/// + 绝对路径（Windows 反斜杠要转成正斜杠，否则 SQLAlchemy 解析不了）。
const posix = value => value.replace(/\\/g, '/');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: /formal-.*\.spec\.js/,
  // 用例之间共享同一个服务与数据库，且都在同一张画布上操作 —— 必须串行。
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: {timeout: 7_000},
  reporter: [['list']],
  use: {
    baseURL: BASE_URL,
    viewport: {width: 1440, height: 960},
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `"${PYTHON}" -m uvicorn image_hub.app:app --host 127.0.0.1 --port ${PORT}`,
    url: `${BASE_URL}/health`,
    cwd: __dirname,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
    env: {
      ...process.env,
      PYTHONPATH: path.join(__dirname, 'src'),
      PYTHONUTF8: '1',
      IMAGE_HUB_ENV: 'development',
      IMAGE_HUB_DATABASE_URL: `sqlite:///${posix(path.join(RUNTIME, 'e2e.db'))}`,
      IMAGE_HUB_STORAGE_ROOT: posix(path.join(RUNTIME, 'storage')),
      // 必须 ≥32 字符：config.ensure_directories() 会断言，短了服务直接起不来。
      IMAGE_HUB_SESSION_SECRET: SESSION_SECRET,
      IMAGE_HUB_BOOTSTRAP_ADMIN_USERNAME: 'admin',
      IMAGE_HUB_BOOTSTRAP_ADMIN_PASSWORD: 'test-password',
      // 画布契约用例不提交生成任务；关掉 worker 以防误打真实上游。
      IMAGE_HUB_WORKER_ENABLED: 'false',
    },
  },
});
