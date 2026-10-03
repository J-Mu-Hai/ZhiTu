'use client';
import { useRef, useState } from 'react';
import { Grip, X, ScrollText } from 'lucide-react';
import { ConversationPanel } from './ConversationPanel';
import { AgentActivityBar } from './AgentActivityBar';
import { useDemo } from '@/features/growth/provider';

/**
 * 工作台上的浮动对话 Dock。
 *
 * ## 等待回复的那一行状态为什么在**这里**,而不是在对话面板里
 *
 * 规范 6.1 说它是一个"顶部状态"。对话面板自己也有一个 `<header>`,把状态写在那里
 * 看起来是最自然的位置 —— 但那一条路是**死的**:`demo2.css` 里
 * `.floating-conversation .conversation-header{display:none}`,而 specificity 上
 * (0,2,0) 压得过 `globals.css` 的 `.conversation-header`(0,1,0)。这个面板**只**在
 * 这个 Dock 里被渲染过,所以写在那里的状态一次都不会出现 —— 一个永远不会执行的
 * 分支比没有更坏:它看起来是做了的。
 *
 * 于是它落到这里:`.floating-title` 是 Dock 上真正看得见的那一行,也是它的"顶部"。
 *
 * ## 唯一的渲染条件是 `sending`
 *
 * `sending` 由 `provider` 直接给出 —— 就是那次**真的在飞**的请求。所以"AI 没在跑
 * 的时候出现「正在思考」"在结构上就不可能发生。这一版只有这一个形态、这一句文案:
 * 规范 6.1 那张表里的三个阶段("正在读取当前节点与相关内容 / 正在分析目标、关系与
 * 限制 / 正在整理建议")后端现在没有暴露,前端也没有任何办法知道走到了哪一步,
 * 所以一个都不写。降级(规则兜底)与失败则由**消息上的来源徽标和错误行**如实说明,
 * 不靠这里的动画区分。
 */
export function FloatingConversation({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [offset, setOffset] = useState({x:0,y:0});
  const { sending, openTrace, traceAvailability, refreshTrace } = useDemo();
  const traceView = traceAvailability.status === 'enabled' ? traceAvailability.view : null;
  const traceRunning = Boolean(traceView?.turns.some(turn => turn.status === 'running'));
  const traceFailed = Boolean(traceView?.turns.some(turn => turn.status === 'failed' || turn.status === 'timed_out'));
  // 只有“可用”才显示正常入口;关闭时整块不渲染;不可用时显示可理解的小状态。
  const traceAvailable = traceAvailability.status === 'enabled';
  const traceUnavailable = traceAvailability.status === 'unavailable';
  const drag = useRef<{x:number;y:number;left:number;top:number}|null>(null);
  return <div className="conversation-overlay" hidden={!open}>
    <section className="floating-conversation" aria-label="浮动对话卡片群" style={{transform:`translate(${offset.x}px, ${offset.y}px)`}}>
      <div className="floating-title"><button className="conversation-move" aria-label="整体移动对话" onPointerDown={e=>{e.currentTarget.setPointerCapture(e.pointerId);drag.current={x:e.clientX,y:e.clientY,left:offset.x,top:offset.y};}} onPointerMove={e=>{const d=drag.current;if(d)setOffset({x:Math.max(-220,Math.min(20,d.left+e.clientX-d.x)),y:Math.max(-12,Math.min(32,d.top+e.clientY-d.y))});}} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}} onKeyDown={e=>{if(e.key.startsWith('Arrow')){e.preventDefault();setOffset(p=>({x:Math.max(-220,Math.min(20,p.x+(e.key==='ArrowLeft'?-10:e.key==='ArrowRight'?10:0))),y:Math.max(-12,Math.min(32,p.y+(e.key==='ArrowUp'?-10:e.key==='ArrowDown'?10:0)))}));}}}><Grip size={15}/>一起思考 <small>拖动此处，整体移动</small></button>
        {sending && (
          <span className="ai-thinking" role="status">
            <i aria-hidden="true" />
            正在思考…
          </span>
        )}
        {traceAvailable && (
          <button
            type="button"
            className="icon-button trace-entry"
            aria-label="运行记录"
            title="运行记录"
            data-testid="trace-entry"
            onClick={openTrace}
          >
            <ScrollText size={15} />
            {traceRunning && <span className="trace-dot trace-dot-running" aria-hidden="true" />}
            {!traceRunning && traceFailed && <span className="trace-dot trace-dot-error" aria-hidden="true" />}
          </button>
        )}
        {traceUnavailable && (
          <span className="trace-unavailable" data-testid="trace-unavailable">
            <button type="button" className="trace-unavailable-detail" onClick={openTrace}>
              运行记录暂不可用
            </button>
            <button type="button" className="trace-unavailable-retry" onClick={() => void refreshTrace()}>
              重试连接
            </button>
          </span>
        )}
        <button className="icon-button" aria-label="让对话内容消失" onClick={onClose}><X size={15}/></button></div>
      <AgentActivityBar/>
      <ConversationPanel/>
    </section>
  </div>;
}
