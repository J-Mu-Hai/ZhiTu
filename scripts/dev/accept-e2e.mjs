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
 * 4. 把 `TEST_PORT` / `API_BASE` / `CI=1` / `ZHITU_RUN_DIR` 交给 Playwright,由它来起
 *    `next start`(`CI=1` 是必须的:它让 `reuseExistingServer` 变成 false,杜绝复用 5173)。
 *
 * ## 每一次运行留下自己的现场,上一次的不许被覆盖
 *
 * 这一次运行的一切都落在 `apps/web/artifacts/runs/<运行编号>/` 一个目录里:
 *
 * | 文件 | 是什么 |
 * | --- | --- |
 * | `summary.txt` | **这一次的记录**:运行编号、提交、工作区是否干净、命令、端口、库、耗时、失败清单 |
 * | `report.json` | Playwright 的 JSON 报告(机器可读的那份) |
 * | `test-results/` | 失败截图、trace,以及每条测试自己的输出 |
 * | `backend.log` / `build.log` | 后端启动日志与前端构建日志 |
 * | `zhitu_e2e.db` | **只在失败时**拷进来的测试库快照(含 `-wal`/`-shm`) |
 *
 * 运行编号 = `<本机时间戳>-<提交号>`。**提交号只是这一刻工作区的标签,不是证据本身** ——
 * 跑的时候工作区可能是脏的,所以 `summary.txt` 里另有一行如实写"工作区:干净 / 有 N 项
 * 未提交改动"。一个提交号被当成"跑的就是这份代码"是最容易被读错的一件事。
 *
 * ## 用法
 *
 * ```bash
 * node scripts/dev/accept-e2e.mjs                 # 默认 --workers=1
 * node scripts/dev/accept-e2e.mjs --workers=4     # 对比并发行为
 * node scripts/dev/accept-e2e.mjs --keep          # 连临时目录一起保留(失败时本来就保留)
 * node scripts/dev/accept-e2e.mjs --serve         # 只把栈起起来,不跑测试(留给探针用)
 * ```
 *
 * 端口可用环境变量覆盖:`ZHITU_E2E_API_PORT`(默认 8100)、`ZHITU_E2E_WEB_PORT`(默认 5273)。
 * Python 解释器用 `ZHITU_PYTHON` 覆盖(默认 conda 环境的 zhitu)。
 */

import { spawn } from 'node:child_process';
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join } from 'node:path';
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
// `DATABASE_URL` 用绝对路径,且反斜杠要转成斜杠 —— Windows 上的 `sqlite:///E:\...`
// 会被 SQLAlchemy 当成相对路径解析。
const databaseUrl = `sqlite+aiosqlite:///${dbPath.replaceAll('\\', '/')}`;

// 放在这里而不是下面 `run()` 旁边:上面那段建现场目录的代码要**先**调一次 git,
// 而 `run()` 会往这个集合里塞子进程。`const` 晚一步就是 TDZ 报错。
const children = new Set();

/**
 * **每一次运行一个现场目录,一次运行一份记录。**
 *
 * 这里以前把截图、trace 放在一个固定路径下(`apps/web/artifacts/test-results`),
 * 后果是后一轮会**无声地**盖掉前一轮的 —— 而失败之后第一个要问的恰恰是"上一轮
 * 失败在哪一步"。`docs/08-DEPLOYMENT.md` 第四节记的那五轮里,第 4 轮的现场就是这么
 * 丢的,取不回来。那次没有补造截图,只如实留了说明;现在补的是**机制**。
 *
 * 目录名里带提交号:**提交号是这一刻工作区的标签,不是证据本身** —— 跑的时候工作区
 * 可能是脏的,这一点由 `summary.txt` 里的 `工作区` 一行如实说明(见 `writeSummary`)。
 */
const startedAt = new Date();
const pad = (value) => String(value).padStart(2, '0');
const stamp =
  `${startedAt.getFullYear()}${pad(startedAt.getMonth() + 1)}${pad(startedAt.getDate())}` +
  `-${pad(startedAt.getHours())}${pad(startedAt.getMinutes())}${pad(startedAt.getSeconds())}`;
const headCommit = await commit();
const runId = `${stamp}-${headCommit}`;
const runDir = join(webDir, 'artifacts', 'runs', runId);
const apiLog = join(runDir, 'backend.log');
const buildLog = join(runDir, 'build.log');
const reportPath = join(runDir, 'report.json');
const summaryPath = join(runDir, 'summary.txt');

mkdirSync(runDir, { recursive: true });

/** 这一刻的工作区干净吗。`git status --porcelain` 有输出 = 有未提交改动。 */
async function workingTreeState() {
  const result = await run('git', ['status', '--porcelain']);
  const changed = result.output.split('\n').filter((line) => line.trim() !== '');
  return changed.length === 0 ? '干净' : `有 ${changed.length} 项未提交改动`;
}

/**
 * 把这一次写成 `summary.txt` —— 现场目录的封面。
 *
 * 没有它,"31 passed / 1 skipped" 这句话就只活在终端回滚缓冲区里,而那一句正是
 * 以后要拿来复述的东西。档里写清**提交、工作区是否干净、命令、端口、库、时间、
 * 失败清单**:少了"工作区是否干净",一个提交号会被读成"跑的就是这份代码"。
 */
function writeSummary({ finishedAt, exitCode, report }) {
  const stats = report?.stats;
  const failures = collectFailures(report);
  const total = stats ? stats.expected + stats.unexpected + stats.skipped + stats.flaky : null;
  const lines = [
    '知途 · 端到端验收现场(隔离栈)',
    '',
    `运行编号    ${runId}`,
    `提交        ${headCommit}`,
    `工作区      ${treeState}`,
    `开始         ${startedAt.toISOString()}  (本机 ${startedAt.toLocaleString()})`,
    `结束         ${finishedAt.toISOString()}`,
    `耗时         ${((finishedAt - startedAt) / 1000).toFixed(1)}s`,
    '',
    `命令        npx playwright test ${forwarded.join(' ')}`,
    `前端        http://127.0.0.1:${webPort}  (production 构建, 不复用 5173)`,
    `后端        http://127.0.0.1:${apiPort}  (独立数据库, 无模型 key)`,
    `测试数据库  ${dbSnapshot ? join(runDir, basename(dbPath)) : dbPath}`,
    '',
    `Playwright 退出码  ${exitCode}`,
  ];
  if (total === null) {
    // 没有 report.json 就说明连测试都没跑起来(构建失败、栈起不来)。**不写"0 失败"** ——
    // 那会把"没跑"读成"全过"。
    lines.push('结果        没有跑到测试那一步,没有 report.json。见上面的构建/后端日志。');
  } else if (total === 0) {
    // 报告存在但一条测试都没有:过滤器写错、路径写错、文件被改名都会长成这样。
    // 它和"全过"在数字上都是"0 失败",含义却相反,所以单独说。
    lines.push('结果        report.json 里一条测试都没有 —— 别把这条读成"0 失败",它更可能是没选中任何用例。');
  } else {
    lines.push(
      `结果        ${total} 条:${stats.expected} passed / ${stats.unexpected} failed / ` +
        `${stats.skipped} skipped / ${stats.flaky} flaky`,
    );
    if (failures.length) {
      lines.push('', '失败的用例(截图与 trace 在 test-results/ 下同名目录里):');
      lines.push(...failures);
    } else if (stats.unexpected > 0) {
      lines.push('', '有失败,但 report.json 里没定位到具体用例 —— 别把它读成"没有失败"。');
    }
  }
  writeFileSync(summaryPath, `${lines.join('\n')}\n`);
}

/** 从 Playwright 的 JSON 报告里挑出没通过的用例。只认 `unexpected`,不认"没跑到"。 */
function collectFailures(report) {
  const found = [];
  const walk = (suite, trail) => {
    const path = [...trail, suite.title].filter(Boolean);
    for (const spec of suite.specs ?? []) {
      const bad = (spec.tests ?? []).filter((test) => test.status === 'unexpected');
      if (bad.length) found.push(`  - ${[...path, spec.title].join(' › ')}`);
    }
    for (const child of suite.suites ?? []) walk(child, path);
  };
  for (const suite of report?.suites ?? []) walk(suite, []);
  return found;
}

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
//: 跑之前那一刻的工作区状态,以及测试库有没有被拷进现场目录。两个都由
//: `summary.txt` 引用 —— 缺了前一个,一个提交号会被读成"跑的就是这份代码"。
let treeState = '未取到';
let dbSnapshot = false;

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
  treeState = await workingTreeState();
  banner([
    '知途 · 端到端验收（隔离栈）',
    `运行编号  ${runId}`,
    `提交      ${headCommit}  (工作区:${treeState})`,
    `时间      ${startedAt.toISOString()}`,
    `前端      http://127.0.0.1:${webPort}  (production 构建, 不复用 5173)`,
    `后端      http://127.0.0.1:${apiPort}  (独立数据库, 无模型 key)`,
    `数据库    ${dbPath}`,
    `并发      ${forwarded.find((a) => a.startsWith('--workers'))}`,
    `现场目录  ${runDir}`,
    `临时目录  ${workDir}`,
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
      // 现场目录由**这里**决定(见 `playwright.config.ts` 里 `resolveRunDir`):
      // 截图、trace 落在 `${runDir}/test-results`,JSON 报告落在 `${reportPath}`。
      // 不传的话配置会自己按时间戳建一个,那样日志、报告、截图就分散在两处了。
      ZHITU_RUN_DIR: runDir,
      PLAYWRIGHT_JSON_OUTPUT: reportPath,
    },
    echo: true,
    shell: process.platform === 'win32',
  });
  exitCode = playwright.code ?? 1;
} catch (cause) {
  console.error(`\n验收栈失败:${cause.message}`);
} finally {
  cleanup();

  // 失败要留得下**完整**的现场:测试库也拷进现场目录,这样"当时库里是什么样"不用
  // 再从临时目录里找。库很小(几百 KB),而一个取不回来的失败现场代价很大。
  if (exitCode !== 0 && existsSync(dbPath)) {
    try {
      copyFileSync(dbPath, join(runDir, basename(dbPath)));
      // SQLite 的 WAL 侧车文件:后端是被 kill 掉的(不会再做 checkpoint),
      // 最近的写入很可能只在这些文件里。少拷一个,"当时的库"就是缺一段的。
      for (const suffix of ['-wal', '-shm']) {
        if (existsSync(`${dbPath}${suffix}`)) {
          copyFileSync(`${dbPath}${suffix}`, join(runDir, `${basename(dbPath)}${suffix}`));
        }
      }
      dbSnapshot = true;
    } catch (cause) {
      console.error(`测试库没能拷进现场目录(${cause.message}) —— 它仍在 ${workDir}`);
    }
  }

  // 摘要**总要**写:成功的那一轮也要留下"什么提交、什么结果"。只在报告存在时才
  // 说得清条数,所以读不到就如实写"没跑到测试那一步"。
  let report = null;
  try {
    if (existsSync(reportPath)) report = JSON.parse(readFileSync(reportPath, 'utf8'));
  } catch (cause) {
    console.error(`report.json 读不出来(${cause.message}) —— 摘要里会按"读不到"写`);
  }
  try {
    writeSummary({ finishedAt: new Date(), exitCode, report });
  } catch (cause) {
    console.error(`摘要没写成(${cause.message})`);
  }

  console.log(`\n现场目录:${runDir}`);
  if (report === null) console.log('  没跑到测试那一步 —— 先看 build.log / backend.log');
  console.log(`  这一次的记录  ${summaryPath}`);
  console.log(`  截图与 trace  ${join(runDir, 'test-results')}`);
  console.log(`  后端日志      ${apiLog}`);
  console.log(`  构建日志      ${buildLog}`);
  if (dbSnapshot) console.log(`  测试库快照    ${join(runDir, basename(dbPath))}`);

  if (keep || serve || exitCode !== 0) {
    // 失败时**不删**临时目录:里面除了库还有别的东西(日志、构建中间产物),
    // 而"失败了但现场被清掉"是最难补救的一种损失。
    console.log(`  临时目录 ${workDir}${exitCode !== 0 ? '  (失败,保留)' : ''}`);
  } else {
    // 只删我们自己建的临时目录。删不掉不算失败(Windows 上句柄释放有延迟)。
    try { rmSync(workDir, { recursive: true, force: true }); } catch { /* 留给系统清理 */ }
  }
}

process.exit(exitCode);
