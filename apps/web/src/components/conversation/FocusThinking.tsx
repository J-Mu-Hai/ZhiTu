'use client';
import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { ConversationPanel } from './ConversationPanel';

/**
 * 居中「专注思考」模式。
 *
 * ## 它为什么必须 Portal 到 `document.body`
 *
 * 它曾经是 `.workbench-body` 的孩子,靠内联的 `position:fixed` 假装浮在视口上。
 * `fixed` 只在**没有祖先**建立包含块时才相对视口定位,而工作台这棵子树里有
 * `transform` / `contain` / `backdrop-filter` 这类属性就会把它拽回那个祖先 ——
 * 弹层于是卡在画布右侧、被顶部导航与 Dock 一起裁掉。Portal 到 `body` 之后,
 * 它的定位与父级再没有任何关系。
 *
 * ## 它和右侧 Dock 的关系
 *
 * **同一时刻只能有一个主交互容器。** 弹层打开时 `FloatingConversation` 会把自己
 * 的 `ConversationPanel` 收成空壳(不卸载,草稿留在外面),完整内容只在这里出现。
 * 关闭 / 回答 / 确认 / Esc 都走 `closeFocusThinking`,消息、当前问题与草稿都在
 * provider / 草稿存储里,不会丢。
 */
export function FocusThinking() {
  const { focusThinking, closeFocusThinking } = useDemo();
  // Esc 关闭。挂在 window 上,弹层里输入框有焦点时也能收到。
  useEffect(() => {
    if (!focusThinking) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeFocusThinking();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [focusThinking, closeFocusThinking]);

  if (!focusThinking || typeof document === 'undefined') return null;

  return createPortal(
    <div
      className="focus-modal-root"
      data-testid="focus-thinking"
    >
      {/* 独立 backdrop:它只负责压暗,不负责居中 —— 居中交给 root 的 grid。 */}
      <div className="focus-modal-backdrop" aria-hidden="true" onPointerDown={closeFocusThinking} />
      <section className="focus-modal" role="dialog" aria-modal="true" aria-label="专注思考">
        <header className="focus-modal-header">
          <strong className="focus-modal-title">专注思考</strong>
          <button
            type="button"
            className="focus-modal-close"
            data-testid="focus-thinking-close"
            onClick={closeFocusThinking}
          >
            <X size={13} />回到画布
          </button>
        </header>
        <div className="focus-modal-body">
          <ConversationPanel />
        </div>
      </section>
    </div>,
    document.body,
  );
}
