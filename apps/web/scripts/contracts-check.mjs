#!/usr/bin/env node
/*
 * 前端的接口类型和后端契约对得上吗?
 *
 * ## 这个脚本要挡住的是一种**不会报错的**漂移
 *
 * 后端改了契约(加一个字段、改个名字),`backend/tests/test_contract_drift.py` 会
 * 逼着人重新生成 `shared/schemas/domain.schema.json` —— 那一段是闭环的。但
 * `src/lib/backend.ts` 里的 44 个 interface 是**手写的**,没有任何东西检查它。
 *
 * 于是漂移只能靠人记得。忘了会怎样?两端都不报错:
 *   - 后端加了 `truncated` → 前端没加 → `tsc` 说"没这个属性" (`any` 之外),但
 *     如果前端写的是 `view.truncated ?? false`,它永远取到 false,提示永不出现。
 *   - 后端改名 `weeklyAvailableMinutes` → 前端没改 → 读到的永远是 `undefined`,
 *     界面上那一栏就一直是空的。
 *
 * 后一种是这里真正要抓的:**类型在撒谎**。它声称某个字段一定有值,而那个字段
 * 从来不会到达。TS 对此无能为力,因为它只看得见自己的那份声明。
 *
 * ## 为什么不做成"从 schema 生成 TS"
 *
 * `json-schema-to-typescript` 能把 `.json` 变成 `.d.ts`,但那要求 `backend.ts`
 * 整体改成生成物 —— 44 个 interface、别名、`Partial<>`、字面量联合(后端契约里
 * 表达不了 `'completed' | 'partial' | ...`)。生成器往返不干净,而且会把一份能读
 * 的文件变成一堆需要对照着看的产物。这里只需要**发现不一致**,不需要自动改写:
 * 不一致是少数情况,改哪一边、怎么改,该由人决定。
 *
 * ## 用法
 *
 *     npm run contracts:check
 *
 * 退出码非 0 就是漂移了,输出会指名道姓列出差在哪。
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const SCHEMA_PATH = resolve(here, '../../../shared/schemas/domain.schema.json');
const TYPES_PATH = resolve(here, '../src/lib/backend.ts');

/**
 * 把 `export interface X ... { ... }` 一个个抠出来,顺带记下它继承了谁。
 *
 * 手写一个括号扫描器而不是上 TS 编译器,是因为这里只需要**字段名**这一件事:
 * 引 `typescript` 进来解析一整棵树,为的是读 44 个 interface 的属性名,代价和
 * 收益不成比例。扫描器只认两种形态(`extends` 和 `name?: type;`),遇到不认识的
 * 行**跳过并在最后报告跳过数** —— 静默跳过会让这个检查假装自己覆盖了全部。
 */
function parseInterfaces(source) {
  const found = new Map();
  const skipped = [];
  const header = /export\s+interface\s+([A-Za-z0-9_]+)\s*(?:extends\s+([A-Za-z0-9_]+)\s*)?\{/g;

  let match;
  while ((match = header.exec(source)) !== null) {
    const [, name, parent] = match;
    let depth = 1;
    let index = header.lastIndex;
    const start = index;
    while (index < source.length && depth > 0) {
      const char = source[index];
      if (char === '{') depth += 1;
      else if (char === '}') depth -= 1;
      index += 1;
    }
    const body = source.slice(start, index - 1);
    header.lastIndex = index;

    const props = new Set();
    for (const raw of body.split('\n')) {
      const line = raw.trim();
      if (!line || line.startsWith('//') || line.startsWith('/*') || line.startsWith('*')) continue;
      // 属性行:`name?: type` / `name: type` / `'kebab-name': type`。
      const prop = /^(?:'([^']+)'|([A-Za-z0-9_$]+))\s*\??\s*:/.exec(line);
      if (prop) {
        props.add(prop[1] ?? prop[2]);
        continue;
      }
      // 方法签名、索引签名等。记下来,最后一起说。
      skipped.push(`${name}: ${line}`);
    }
    found.set(name, { props, parent: parent ?? null });
  }
  return { found, skipped };
}

/** 算上一个 interface 的**全部**字段,包括从父接口继承来的。 */
function allProps(name, interfaces, seen = new Set()) {
  const entry = interfaces.get(name);
  if (!entry || seen.has(name)) return new Set();
  seen.add(name);
  const inherited = entry.parent ? allProps(entry.parent, interfaces, seen) : new Set();
  return new Set([...inherited, ...entry.props]);
}

function main() {
  const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf8'));
  const defs = schema.$defs ?? {};
  const source = readFileSync(TYPES_PATH, 'utf8');
  const { found: interfaces, skipped } = parseInterfaces(source);

  const problems = [];
  let compared = 0;

  for (const [name, def] of Object.entries(defs)) {
    // 只比"前端也声明了"的那些。没声明的可能压根用不上(不比对,但也不假装比过)。
    if (!interfaces.has(name)) continue;
    // 联合类型等没有 properties 的定义交给别的检查。
    const expected = def.properties;
    if (!expected) continue;
    compared += 1;

    const actual = allProps(name, interfaces);
    const missingInTs = Object.keys(expected).filter(key => !actual.has(key));
    const extraInTs = [...actual].filter(key => !(key in expected));

    if (missingInTs.length) {
      problems.push(
        `${name}: 后端会返回、但 backend.ts 里没写 —— ${missingInTs.join(', ')}\n` +
          `    (这一条**不是**错误,是遗漏:类型少写一个字段,TS 会挡住所有读它的代码\n` +
          `      —— 后端给了数据,前端够不着,而且没有任何地方会说。要么补上,要么确认这个\n` +
          `      interface 只是用到的一小部分,并把它挪出 backend.ts。)`,
      );
    }
    if (extraInTs.length) {
      problems.push(
        `${name}: backend.ts 里写了、但后端不会返回 —— ${extraInTs.join(', ')}\n` +
          `    (这一条是**真的在撒谎**:类型声称有值,而那个字段永远不会到达,\n` +
          `     读到的恒为 undefined。TS 查不出这种错。)`,
      );
    }
  }

  const shared = Object.keys(defs).filter(name => interfaces.has(name)).length;
  console.log(
    `契约检查:${compared} 个 interface 逐字段比对完成` +
      `(后端共 ${Object.keys(defs).length} 个定义,其中 ${shared} 个前端也有声明)。`,
  );
  if (skipped.length) {
    console.log(`跳过 ${skipped.length} 行无法识别的声明(方法/索引签名):`);
    for (const line of skipped) console.log(`  - ${line}`);
  }

  if (problems.length) {
    console.error(`\n发现 ${problems.length} 处漂移:\n`);
    for (const problem of problems) console.error(`  ✗ ${problem}`);
    console.error(
      '\n改哪一边由你定 —— 后端契约是权威,但前端也可能是在表达一个自己的视图模型' +
        '(那样的话把它挪出 backend.ts,改用别的名字)。',
    );
    process.exit(1);
  }

  console.log('没有漂移。');
}

main();
