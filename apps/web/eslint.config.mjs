import { FlatCompat } from '@eslint/eslintrc';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const compat = new FlatCompat({ baseDirectory: path.dirname(fileURLToPath(import.meta.url)) });
const config = [
  // .next-dev 是本地 `next dev` 的产物目录(已在 .gitignore 里),不是我们的代码。
  // 之前只忽略了 .next,于是 `npm run lint` 会在生成的 JS 上扫出 400 多个错,
  // 把 src 里真正的问题淹掉。
  { ignores: ['.next/**', '.next-dev/**', 'node_modules/**', 'next-env.d.ts', 'artifacts/**'] },
  ...compat.extends('next/core-web-vitals', 'next/typescript'),
];
export default config;
