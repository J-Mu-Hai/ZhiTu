'use client';
import { useRouter, useSearchParams } from 'next/navigation';
import { GitBranch, ChartNoAxesGantt, ListTodo, CalendarRange, PanelRightOpen, ArrowLeft } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { PathView } from '@/components/growth/PathView';
import { TimelineView } from './TimelineView';
import { TaskView } from './TaskView';
import { ScheduleView } from './ScheduleView';
import { FloatingConversation } from '@/components/conversation/FloatingConversation';
import { AgentTraceInspector } from '@/components/conversation/AgentTraceInspector';
import { useDemo } from '@/features/growth/provider';
import { useMobileLayout } from '@/lib/media';
export function Workbench() {
  const params = useSearchParams(); const router = useRouter();
  const value = params.get('view'); const view = value === 'timeline' || value === 'tasks' || value === 'schedule' ? value : 'path';
  /*
   * ## 手机上面板默认收起
   *
   * 面板在手机上是一张从底部升起的抽屉(见 `ui-refresh.css` 里那一档),默认展开的话
   * 画布只剩顶部一条 —— 实测 390×844 上它占掉 y=288 往下全部,根节点(在 y=400)
   * 整个盖住,"打开工作台看见的第一样东西是一大片对话空状态"。所以窄屏的默认值是
   * **收起**,要问再点开。
   *
   * 拆成"用户选过的值"和"默认值"两件事:`chatChoice` 一开始是 `null`(没选过),
   * 于是跟着断点走;用户点过一次之后它就是 `true`/`false`,**不再随窗口宽度变化** ——
   * 否则转一下屏幕就会把用户刚打开的面板收掉。
   */
  const narrow = useMobileLayout();
  const [chatChoice, setChatChoice] = useState<boolean | null>(null);
  const chatOpen = chatChoice ?? !narrow;
  const { growth, spaceId, canvasKey, enterSpace, workspaceId, reasoning } = useDemo();
  // **根目标不叫 `'goal'`。** 那是 `emptyGrowth` 用的哨兵值,只有在计划还没从后端
  // 拿到的时候才存在;真实空间拿到计划之后,根节点的 id 是一个 UUID。所以拿
  // `spaceId !== 'goal'` 当"我是不是在根这一层"来判断,在真实空间里恒为真。
  const isRootSpace = spaceId === growth.goalId;
  /*
   * 视图切换。窄屏那一档把标签收进 `<span>` 里(只留图标 + 当前项的文字),
   * 而 `display:none` 的文字**不进可访问性树** —— 所以按钮上必须同时挂
   * `aria-label`,否则手机上这四个入口对读屏来说是四个没有名字的按钮。
   * 宽屏时 `aria-label` 和可见文字是同一个字符串,没有副作用。
   */
  const tabs = [{ id: 'path', label: '路径', Icon: GitBranch }, { id: 'timeline', label: '时间线', Icon: ChartNoAxesGantt }, { id: 'tasks', label: '任务', Icon: ListTodo }, { id: 'schedule', label: '排期', Icon: CalendarRange }];
  const workspaceParam = `workspace=${encodeURIComponent(workspaceId)}`;
  /*
   * 阶段 11:**新的战略时间架构版本生成后,自动切到时间线一次。**
   *
   * “一次”的边界是 `mapVersion`:同一个版本用户手动切回路径页后不再抢页面;
   * 只有服务端给出**新版本**(用户又答了一轮、架构变了)才再切一次。
   */
  const architectureVersion =
    reasoning &&
    reasoning.nodes.some(node => node.nodeType === 'route') &&
    reasoning.nodes.some(node => node.nodeType === 'stage')
      ? reasoning.mapVersion
      : 0;
  const switchedArchitectureRef = useRef(0);
  useEffect(() => {
    if (architectureVersion <= 0) return;
    if (switchedArchitectureRef.current === architectureVersion) return;
    switchedArchitectureRef.current = architectureVersion;
    if (view !== 'timeline') {
      router.replace(`/workbench?${workspaceParam}&view=timeline`, { scroll: false });
    }
  }, [architectureVersion, view, router, workspaceParam]);
  return <div className="workbench open-workbench"><div className={`workbench-body ${chatOpen ? '' : 'chat-hidden'}`}><section className="workspace">
    <div className="space-topbar">{!isRootSpace && <button className="icon-button space-back" aria-label="返回上级空间" onClick={() => enterSpace(growth.nodes[spaceId]?.parentId ?? growth.goalId)}><ArrowLeft size={15}/></button>}<div className="view-tabs" role="tablist" aria-label="工作台视图">{tabs.map(({id,label,Icon}) => <button key={id} role="tab" aria-selected={view === id} aria-label={label} className={view === id ? 'selected' : ''} onClick={() => router.replace(`/workbench?${workspaceParam}&view=${id}`, { scroll: false })}><Icon size={15}/><span>{label}</span></button>)}</div>{!chatOpen && <button className="icon-button reopen-chat" aria-label="展开对话" onClick={() => setChatChoice(true)}><PanelRightOpen size={18}/></button>}</div>
    {/* 视图是一个三元表达式,所以**切一下页签,整棵画布子树就被卸载了** —— 这是有意的:
        画布和列表不该抢同一个位置,而隐藏着不卸载会让 ReactFlow 拿到一个尺寸为 0 的容器。
        代价是画布内部的东西(平移缩放、弹窗里没提交的输入)会跟着没,所以那两样都
        存在组件外面:视口在 Provider(`viewports`),输入在 `features/growth/drafts.ts`。
        `key` 用的是 `canvasKey` 而不是 `spaceId`,理由见 Provider 里 `canvasKey` 那段。 */}
    <div className="view-content">{view === 'path' ? <PathView key={canvasKey}/> : view === 'timeline' ? <TimelineView/> : view === 'schedule' ? <ScheduleView/> : <TaskView/>}</div></section><FloatingConversation open={chatOpen} onClose={() => setChatChoice(false)}/><AgentTraceInspector/></div></div>;
}
