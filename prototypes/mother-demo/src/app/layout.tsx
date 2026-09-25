import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { DemoProvider } from '@/features/growth/provider';
import { AppShell } from '@/components/shell/AppShell';
import '@xyflow/react/dist/style.css';
import './globals.css';
import './experience.css';
export const metadata: Metadata = { title: '知途 · 让成长有迹可循', description: '大学生成长规划 · 交互体验 Demo' };
export default function Layout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body><DemoProvider><AppShell>{children}</AppShell></DemoProvider></body></html>;
}
