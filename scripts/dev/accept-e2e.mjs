#!/usr/bin/env node
/**
 * 正式验收用的**隔离**端到端测试栈。
 *
 * ## 为什么需要它,而不是直接 `npm run test:e2e`
 *
 * `apps/web/playwright.config.ts` 里 `reuseExistingServer: !process.env.CI` ——
 * 本地跑的时候它**会默默复用 5173 上的 `next dev`**。于是有两种很坏的可能:
 *
 * 1. 你以为在跑验收,其实在跑一个按需编译的开发服务器,4 个 worker 一起压它,
 *    失败信息是 `page.goto` 超时,和产品毫无关系;
 * 2. 你以为在验当前这次改动,其实那台 dev server 已经被别人改过文件热更新了。
 *
 * 这个脚本把口径钉死:**production 构建 + 独立端口 + 独立后端 + 独立数据库**,
 * 不复用任何正在跑的进程,也不碰 `data/zhitu_dev.db`。
 *
 * ## 它会做什么
 *
 * 1. 用 `NEXT_PUBLIC_API_BASE_URL` 指向**测试后端**做一次 production 构建
 *    (这个变量是构建期烤进 JS 的,所以必须构建,不能靠运行时注入);
 * 2. 起一个独立的后端:独立端口、独立 SQLite 文件、`LLM_API_KEY` 为空、
 *    reasoner 固定 `rule`、CORS 只放行测试前端端口 —— 所以**不会有真实模型调用**,
 *    也不会写进开发库;
 * 3. 跑 `alembic upgrade head` 建测试库(后端启动时会拒绝 schema 不是 head 的库);
 * 4. 把 `TEST_PORT` / `API_BASE` / `CI=1` 交给 Playwright,由它来起 `next start`
 *    (`CI=1` 是必须的:它让 `reuseExistingServer` 变成 false,杜绝复用 5173)。
 *
 * ## 用法
 *
 * ```bash
 * node scripts/dev/accept-e2e.mjs                 # 默认 --workers=1
 * node scripts/dev/accept-e2e.mjs --workers=4     # 对比并发行为
 * node scripts/dev/accept-e2e.mjs --keep          # 保留临时库与日志,便于查现场
 * node scripts/dev/accept-e2e.mjs --serve         # 只把栈起起来,不跑测试(留给探针用)
 * ```
 *
 * 端口可用环境变量覆盖:`ZHITU_E2E_API_PORT`(默认 8100)、`ZHITU_E2E_WEB_PORT`(默认 5273)。
 * Python 解释器用 `ZHITU_PYTHON` 覆盖(默认 conda 环境的 zhitu)。
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const webDir = join(repoRoot, 'apps', 'web');

const argv = process.argv.slice(2);
const keep = argv.includes('--keep');
/**
 * `--serve` 只把栈起起来,不跑测试。
 *
 * 存在的理由:验收失败之后要能**复现**它。用开发服务器复现是不行的 —— 同一个交互
 * 在 `next dev` 上通过、在 production 上失败,这正是要查的那类问题;查它就必须站在
 * production 那一边。所以这里把同一个隔离栈留在原地,交给探针(见
 * `apps/web/artifacts/probe-*.mjs`)去打。
 */
const serve = argv.includes('--serve');
const forwarded = argv.filter((arg) => arg !== '--keep' && arg !== '--serve');

/**
 * 并发数默认写死 1。
 *
 * 这不是"让测试跑快一点"的旋钮,而是**基线口径**:先要一个可重复的结果。
 * 想对比并发行为时显式传 `--workers=4`,让两次运行在记录里长得不一样。
 */
if (!forwarded.some((arg) => arg.startsWith('--workers'))) forwarded.push('--workers=1');

const apiPort = process.env.ZHITU_E2E_API_PORT ?? '8100';
const webPort = process.env.ZHITU_E2E_WEB_PORT ?? '5273';
const python = process.env.ZHITU_PYTHON ?? 'C:/Users/j/miniconda3/envs/zhitu/python.exe';

if (!existsSync(python)) {
  console.error(`找不到 Python 解释器:${python}\n用 ZHITU_PYTHON 指定 conda 环境 zhitu 的 python。`);
  process.exit(2);
}

const workDir = mkdtempSync(join(tmpdir(), 'zhitu-e2e-'));
const dbPath = join(workDir, 'zhitu_e2e.db');
const apiLog = join(workDir, 'backend.log');
const buildLog = join(workDir, 'build.log');
// `DATABASE_URL` 用绝对路径,且反斜杠要转成斜杠 —— Windows 上的 `sqlite:///E:\...`
// 会被 SQLAlchemy 当成相对路径解析。
const databaseUrl = `sqlite+aiosqlite:///${dbPath.replaceAll('\\', '/')}`;

const children = new Set();

function run(command, args, options = {}) {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(command, args, { cwd: options.cwd ?? repoRoot, env: { ...process.env, ...options.env }, stdio: ['ignore', 'pipe', 'pipe'], shell: options.shell ?? false });
    children.add(child);
    let output = '';
    child.stdout.on('data', (chunk) => { output += chunk; if (options.echo) process.stdout.write(chunk); });
    child.stderr.on('data', (chunk) => { output += chunk; if (options.echo) process.stderr.write(chunk); });
    child.on('error', rejectPromise);
    child.on('close', (code) => { children.delete(child); resolvePromise({ code, output }); });
  });
}

/** 只结束**我们自己起的**子进程。绝不按名字批量杀 node/python。 */
function killTree(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
  } else {
    child.kill('SIGTERM');
  }
}

async function waitFor(url, label, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  let last = '没有响应';
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
      last = `HTTP ${response.status}`;
    } catch (cause) {
      last = cause.message;
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`${label} 在 ${timeoutMs / 1000}s 内没有就绪(轮询 ${url},最后错误:${last})`);
}

async function commit() {
  const result = await run('git', ['rev-parse', '--short', 'HEAD']);
  return result.output.trim() || 'unknown';
}

function banner(lines) {
  const width = Math.max(...lines.map((l) => l.length)) + 2;
  console.log(`\n${'═'.repeat(width)}`);
  for (const line of lines) console.log(` ${line}`);
  console.log(`${'═'.repeat(width)}\n`);
}

let apiProcess = null;
let exitCode = 1;
let cleanedUp = false;

/** 只结束**我们自己起的**子进程,绝不按名字批量杀 node/python。 */
function cleanup() {
  if (cleanedUp) return;
  cleanedUp = true;
  for (const child of [...children]) killTree(child);
}

// `--serve` 会一直停在那儿,所以 Ctrl+C 是它的正常退出路径 —— 那条路径不会走
// `finally`,必须自己收尾,否则 8100 / 5273 上会留下两个没人管的进程。
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    console.log(`\n收到 ${signal},收尾…`);
    cleanup();
    process.exit(130);
  });
}

try {
  banner([
    '知途 · 端到端验收（隔离栈）',
    `提交      ${await commit()}`,
    `时间      ${new Date().toISOString()}`,
    `前端      http://127.0.0.1:${webPort}  (production 构建, 不复用 5173)`,
    `后端      http://127.0.0.1:${apiPort}  (独立数据库, 无模型 key)`,
    `数据库    ${dbPath}`,
    `并发      ${forwarded.find((a) => a.startsWith('--workers'))}`,
    `工作目录  ${workDir}`,
  ]);

  console.log('[1/4] 建测试数据库(schema 必须到 head,否则后端会拒绝启动)');
  const migrate = await run(python, ['-m', 'alembic', '-c', 'backend/alembic.ini', 'upgrade', 'head'], {
    env: { DATABASE_URL: databaseUrl, PYTHONUTF8: '1' },
  });
  if (migrate.code !== 0) throw new Error(`alembic upgrade 失败:\n${migrate.output}`);
  console.log('      迁移完成');

  console.log(`[2/4] 起测试后端 127.0.0.1:${apiPort}`);
  apiProcess = spawn(python, ['-m', 'uvicorn', 'backend.api.main:app', '--host', '127.0.0.1', '--port', apiPort], {
    cwd: repoRoot,
    env: {
      ...process.env,
      DATABASE_URL: databaseUrl,
      // 没有 key 就没有真实模型调用。`rule` 让降级路径也确定,不靠 auto 的探测结果。
      LLM_API_KEY: '',
      AGENT_REASONER: 'rule',
      APP_ENV: 'development',
      APP_SECRET_KEY: 'e2e-only-not-a-secret',
      // 只放行测试前端端口。放行 5173 会让"测试其实打到了开发后端"变得可能。
      CORS_ORIGINS: `http://127.0.0.1:${webPort}`,
      PYTHONUTF8: '1',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.add(apiProcess);
  let apiOutput = '';
  apiProcess.stdout.on('data', (c) => { apiOutput += c; });
  apiProcess.stderr.on('data', (c) => { apiOutput += c; });
  apiProcess.on('close', () => children.delete(apiProcess));

  // 等 `/ready` 而不是 `/health`:`/health` 只是"进程活着",它连数据库都不碰。
  // 验收要的是"能存数据",schema 没到 head 时 `/ready` 会一直 503。
  try {
    await waitFor(`http://127.0.0.1:${apiPort}/ready`, '测试后端就绪探针');
  } catch (cause) {
    writeFileSync(apiLog, apiOutput);
    throw new Error(`${cause.message}\n后端日志(${apiLog}):\n${apiOutput.slice(-4000)}`);
  }
  writeFileSync(apiLog, apiOutput);
  console.log('      后端就绪');

  console.log('[3/4] production 构建(把测试后端的地址烤进前端)');
  const build = await run('npm', ['run', 'build'], {
    cwd: webDir,
    env: { NEXT_PUBLIC_API_BASE_URL: `http://127.0.0.1:${apiPort}` },
    shell: process.platform === 'win32',
  });
  writeFileSync(buildLog, build.output);
  if (build.code !== 0) throw new Error(`前端构建失败(完整日志:${buildLog}):\n${build.output.slice(-3000)}`);
  console.log('      构建完成');

  if (serve) {
    console.log(`[4/4] --serve:起 next start 并停在这里(不跑测试)`);
    const webProcess = spawn('node', ['node_modules/next/dist/bin/next', 'start', '--hostname', '127.0.0.1', '--port', webPort], {
      cwd: webDir,
      // `NODE_ENV=production` 是必须的:`next.config.mjs` 用它决定 distDir,少了它
      // `next start` 会去读 `.next-dev` 那份开发产物,"production 栈"就成了谎话。
      env: { ...process.env, NODE_ENV: 'production' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    children.add(webProcess);
    webProcess.stdout.on('data', (c) => process.stdout.write(c));
    webProcess.stderr.on('data', (c) => process.stderr.write(c));
    webProcess.on('close', () => children.delete(webProcess));
    await waitFor(`http://127.0.0.1:${webPort}`, 'next start');

    console.log(`\n栈已就绪,留在这里等 Ctrl+C:`);
    console.log(`  PROBE_WEB=http://127.0.0.1:${webPort}  PROBE_API=http://127.0.0.1:${apiPort}`);
    console.log(`  数据库 ${dbPath}\n`);
    // 永远不 resolve:进程会一直活到被 Ctrl+C。清理交给下面的 SIGINT 处理器,
    // 因为 `finally` 只在 try 正常结束或抛错时才跑,而这里既没结束也没抛错。
    await new Promise(() => {});
  }

  console.log('[4/4] 跑 Playwright(由它自己起 next start)\n');
  const playwright = await run('npx', ['playwright', 'test', ...forwarded], {
    cwd: webDir,
    env: {
      TEST_PORT: webPort,
      API_BASE: `http://127.0.0.1:${apiPort}`,
      // CI=1 的唯一作用:让 playwright.config.ts 的 reuseExistingServer 变成 false。
      CI: '1',
      // `next.config.mjs` 用 NODE_ENV 决定 distDir(`development` → `.next-dev`),
      // 不显式钉住的话,外层 shell 里恰好设了 NODE_ENV=development 时
      // `next start` 会去 `.next-dev` 拿那份**开发产物**,验收就悄悄变成了开发模式。
      NODE_ENV: 'production',
    },
    echo: true,
    shell: process.platform === 'win32',
  });
  exitCode = playwright.code ?? 1;
} catch (cause) {
  console.error(`\n验收栈失败:${cause.message}`);
} finally {
  cleanup();
  if (keep || serve) {
    console.log(`\n现场保留在:${workDir}\n  后端日志 ${apiLog}\n  构建日志 ${buildLog}`);
  } else {
    // 只删我们自己建的临时目录。删不掉不算失败(Windows 上句柄释放有延迟)。
    try { rmSync(workDir, { recursive: true, force: true }); } catch { /* 留给系统清理 */ }
  }
}

process.exit(exitCode);
