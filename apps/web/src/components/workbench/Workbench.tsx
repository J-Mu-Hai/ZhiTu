'use client';
import { useRouter, useSearchParams } from 'next/navigation';
import { GitBranch, ChartNoAxesGantt, ListTodo, CalendarRange, ChevronRight, PanelRightClose, PanelRightOpen, ArrowLeft, Layers3 } from 'lucide-react';
import { useState } from 'react';
import { PathView } from '@/components/growth/PathView';
import { TimelineView } from './TimelineView';
import { TaskView } from './TaskView';
import { ScheduleView } from './ScheduleView';
import { FloatingConversation } from '@/components/conversation/FloatingConversation';
import { useDemo } from '@/features/growth/provider';
import { spaceTrail } from '@/features/growth/selectors';
export function Workbench() {
  const params = useSearchParams(); const router = useRouter();
  const value = params.get('view'); const view = value === 'timeline' || value === 'tasks' || value === 'schedule' ? value : 'path';
  const [chatOpen, setChatOpen] = useState(true); const { growth, spaceId, canvasKey, enterSpace, workspaceId } = useDemo();
  const trail = spaceTrail(growth, spaceId);
  // **根目标不叫 `'goal'`。** 那是 `emptyGrowth` 用的哨兵值,只有在计划还没从后端
  // 拿到的时候才存在;真实空间拿到计划之后,根节点的 id 是一个 UUID。所以拿
  // `spaceId !== 'goal'` 当"我是不是在根这一层"来判断,在真实空间里恒为真 ——
  // 面包屑上的"返回上级"会在根空间显示,点下去什么也不会发生;而面包屑里根节点那
  // 一格会显示节点的标题(可能是空的)而不是空间名。判断基准只有 `growth.goalId`。
  const isRootSpace = spaceId === growth.goalId;
  const tabs = [{ id: 'path', label: '路径', Icon: GitBranch }, { id: 'timeline', label: '时间线', Icon: ChartNoAxesGantt }, { id: 'tasks', label: '任务', Icon: ListTodo }, { id: 'schedule', label: '排期', Icon: CalendarRange }];
  const workspaceParam = `workspace=${encodeURIComponent(workspaceId)}`;
  return <div className="workbench open-workbench"><div className={`workbench-body ${chatOpen ? '' : 'chat-hidden'}`}><section className="workspace">
    <div className="space-topbar"><div className="space-breadcrumb" aria-label="空间路径"><button className="workspace-switch" onClick={() => router.push('/spaces')}><Layers3 size={14}/>全部空间</button>{!isRootSpace && <button className="icon-button" aria-label="返回上级空间" onClick={() => enterSpace(growth.nodes[spaceId]?.parentId ?? growth.goalId)}><ArrowLeft size={15}/></button>}{trail.map((node,i)=><span key={node.id}>{i>0&&<ChevronRight size={12}/>}<button onClick={()=>enterSpace(node.id)}>{node.id === growth.goalId ? growth.title : node.title}</button></span>)}</div><div className="view-tabs" role="tablist" aria-label="工作台视图">{tabs.map(({id,label,Icon}) => <button key={id} role="tab" aria-selected={view === id} className={view === id ? 'selected' : ''} onClick={() => router.replace(`/workbench?${workspaceParam}&view=${id}`, { scroll: false })}><Icon size={15}/>{label}</button>)}</div><button className="icon-button" aria-label={chatOpen ? '收起对话' : '展开对话'} onClick={() => setChatOpen(!chatOpen)}>{chatOpen ? <PanelRightClose size={18}/> : <PanelRightOpen size={18}/>}</button></div>
    {/* 视图是一个三元表达式,所以**切一下页签,整棵画布子树就被卸载了** —— 这是有意的:
        画布和列表不该抢同一个位置,而隐藏着不卸载会让 ReactFlow 拿到一个尺寸为 0 的容器。
        代价是画布内部的东西(平移缩放、弹窗里没提交的输入)会跟着没,所以那两样都
        存在组件外面:视口在 Provider(`viewports`),输入在 `features/growth/drafts.ts`。
        `key` 用的是 `canvasKey` 而不是 `spaceId`,理由见 Provider 里 `canvasKey` 那段。 */}
    <div className="view-content">{view === 'path' ? <PathView key={canvasKey}/> : view === 'timeline' ? <TimelineView/> : view === 'schedule' ? <ScheduleView/> : <TaskView/>}</div></section><FloatingConversation open={chatOpen} onClose={() => setChatOpen(false)}/></div></div>;
}
