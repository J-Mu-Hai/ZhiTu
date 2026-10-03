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
// 排版与表面令牌。**必须排在 `dark-theme.css` 之后**:那个文件的最后一层
// (`:892`) 是当前生效的暖白变量层,谁排在它前面,谁的 `:root` 就会被静默盖掉 ——
// 不报错、不提示,只是不生效。令牌排在这里,改动才一定看得见。
// 它只有 `:root` 变量、没有选择器规则,所以不会影响任何现有画面。
import './tokens.css';
import './auth.css';
// 排在 dark-theme.css 之后:那个文件的最后一层是当前生效的暖白主题,新组件的
// 样式必须排在它后面才不会被旧规则按顺序盖掉。
import './today-live.css';
import './canvas-polish.css';
import './ui-refresh.css';
// 规划智能体重构 V1(P1):右侧对话面板里“初步思考”的放大输入形态。
// 画布与节点**复用既有 PathView / ReasoningNode / ConversationPanel**,这里不定义独立 UI。
import './v1.css';
// 排在**最后**:统一 Motion Tokens 与 reduced-motion 总开关要盖掉前面那些文件里
// 散落的临时时长(`.18s` / `.2s` / `.15s`)。见 `motion.css` 顶部。
import './motion.css';
export const metadata: Metadata = { title: '知途 · 对话成长空间', description: '面向大学生的长期成长规划工作台' };
export default function Layout({ children }: { children: ReactNode }) {
  return <html lang="zh-CN"><body><AuthProvider><DemoProvider><AppShell>{children}</AppShell></DemoProvider></AuthProvider></body></html>;
}
