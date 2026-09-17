'use client';
import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { Grip, X } from 'lucide-react';
import { ConversationPanel } from './ConversationPanel';
type Size = { width: number; height: number };
export function FloatingConversation({ open, onClose }: { open: boolean; onClose: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<Size>({ width: 350, height: 540 });
  const drag = useRef<{ x: number; y: number; size: Size } | null>(null);
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setSize(old => ({ width: Math.min(old.width, Math.max(0, element.clientWidth - 24)), height: Math.min(old.height, Math.max(0, element.clientHeight - 24)) })));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  function begin(event: PointerEvent<HTMLButtonElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, y: event.clientY, size };
  }
  function resize(width: number, height: number) {
    const bounds = host.current;
    if (!bounds) return;
    const maxWidth = Math.max(0, bounds.clientWidth - 24), maxHeight = Math.max(0, bounds.clientHeight - 24);
    setSize({ width: Math.min(maxWidth, Math.max(Math.min(280, maxWidth), width)), height: Math.min(maxHeight, Math.max(Math.min(300, maxHeight), height)) });
  }
  return <div ref={host} className="conversation-overlay">{open&&<section className="floating-conversation" aria-label="浮动对话窗口" style={{ width: size.width, height: size.height }}>
    <div className="floating-title"><span>一起思考</span><small>拖动左下角调整大小</small><button className="icon-button" aria-label="关闭浮动对话" onClick={onClose}><X size={14}/></button></div>
    <ConversationPanel/>
    <button className="conversation-resize" aria-label="调整对话窗口大小" title="拖动调整宽高；方向键微调" onPointerDown={begin} onPointerMove={event=>{const start=drag.current;if(start)resize(start.size.width+start.x-event.clientX,start.size.height+event.clientY-start.y);}} onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}} onLostPointerCapture={()=>{drag.current=null;}} onKeyDown={event=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();resize(size.width+(event.key==='ArrowLeft'?20:event.key==='ArrowRight'?-20:0),size.height+(event.key==='ArrowDown'?20:event.key==='ArrowUp'?-20:0));}}}><Grip size={15}/></button>
  </section>}</div>;
}

