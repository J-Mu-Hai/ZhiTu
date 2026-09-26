import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { DemoProvider } from '@/features/growth/provider';
import { AuthProvider } from '@/features/auth/provider';
import { AppShell } from '@/components/shell/AppShell';
import '@xyflow/react/dist/style.css';
import './globals.css';
import './experience.css';
import './demo2.css';
import './demo3-repair.css';
import './dark-theme.css';
import './auth.css';
// 排在 dark-theme.css 之后:那个文件的最后一层是当前生效的暖白主题,新组件的
// 样式必须排在它后面才不会被旧规则按顺序盖掉。
import './today-live.css';
export const metadata: Metadata = { title: '知途 · 对话成长空间', description: '面向大学生的长期成长规划工作台' };
export default function Layout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body><AuthProvider><DemoProvider><AppShell>{children}</AppShell></DemoProvider></AuthProvider></body></html>;
}
