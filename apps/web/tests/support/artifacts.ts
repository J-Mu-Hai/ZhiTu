import { join } from 'node:path';

/**
 * **每一轮的现场放在哪 —— 一处决定,谁都不许再写死路径。**
 *
 * `playwright.config.ts` 的 `outputDir` 早就是按轮建目录了,但测试自己主动存的那几张
 * 截图不是:它们的 `path` 是写死的 `artifacts/xxx.png`,于是一轮又一轮互相盖。
 * 后果不是"有点乱":同一台机器上连跑几轮,后一轮会把前一轮的图**无声地**换成自己的,
 * 而"上一轮到底长什么样"恰恰是改动验收时第一个要看的东西。这件事已经真实发生过
 * ——`docs/08-DEPLOYMENT.md` 第四节记的那五轮里,第 4 轮的现场就是这么没的。
 *
 * 所以:**所有落盘的东西都从这里取路径**,包括 trace、报告、以及那几张固定名的截图。
 * 固定名可以留(人一眼认得出是哪一张),但前面必须是这一轮自己的目录。
 *
 * ## 目录名从哪来
 *
 * 1. `ZHITU_RUN_DIR` —— 正式验收由 `scripts/dev/accept-e2e.mjs` 建好并传进来,
 *    里面同时放后端日志、构建日志、JSON 报告和这一次的结果摘要。**正式验收走这条。**
 * 2. 没设时(本地 `npm run test:e2e`)按本机时间戳自己建一个,
 *    所以本地随便跑也不会盖掉别的运行。
 *
 * 函数在这里、不在 `playwright.config.ts` 里,是因为**跑测试的是 worker 进程,
 * 不是主进程**:配置在每个 worker 里都会被重新执行一遍,而测试体要的是一个自己算得出来
 * 的路径。同一个函数两处调用、配合下面 `resolveRunDir` 的写入,两边得到的是同一个目录。
 */
export function resolveRunDir(): string {
  if (process.env.ZHITU_RUN_DIR) return process.env.ZHITU_RUN_DIR;
  // 用**本机时间**而不是 UTC 做目录名:这个名字是给人看的("我刚才跑的那一轮在哪"),
  // 而摘要文件里另有完整的 ISO 时间戳。
  return join('artifacts', 'runs', `${stamp(new Date())}-dev`);
}

/** `20260927-001425` —— 与人看目录的顺序一致(年在前,按名字排序就是按时间排序)。 */
function stamp(now: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}

/**
 * 这一轮的目录,并把它**记进环境变量**。
 *
 * 只有 `playwright.config.ts` 调它,而且必须在任何 worker 起来之前调:worker 是
 * `fork` 出来的、继承当时的 `process.env`,所以先写进去,worker 里再算一次也会
 * 拿到同一个值(见上面第 1 条)。少了这一步,主进程算出的是 12:00:01、worker 算出的是
 * 12:00:03 —— 截图会落进**另一个**目录,而两个目录都长得像"这一轮"。
 */
export function pinRunDir(): string {
  const dir = resolveRunDir();
  process.env.ZHITU_RUN_DIR = dir;
  return dir;
}

/**
 * 这一轮里某个文件该落在哪。`name` 可以是 `canvas-polish.png` 这样的文件名,
 * 也可以是 `nested/thing.json` 这样带子目录的相对路径。
 */
export function artifactPath(name: string): string {
  return join(resolveRunDir(), name);
}
