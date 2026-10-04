'use client';
import { X } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { ConversationPanel } from './ConversationPanel';

/**
 * 居中「专注思考」模式。
 *
 * 它**不是**把右侧 Dock 撑大:右侧 Dock 仍是日常轻量入口;这里是一个居中 Modal,
 * 宽度约 860px、最大高度不超过视口,背景画布保留、只加一层轻遮罩,不挤压画布。
 * 关闭后回到右侧 Dock —— 草稿、消息与当前问题都在 provider 里,不会丢。
 *
 * 内容直接复用 `ConversationPanel`,所以战略判断 / 本轮问题 / 候选方向及后果 /
 * 用户输入都在同一处,不存在第二套对话逻辑。
 */
export function FocusThinking() {
  const { focusThinking, closeFocusThinking } = useDemo();
  if (!focusThinking) return null;
  return (
    <div
      data-testid="focus-thinking"
      onPointerDown={event => { if (event.target === event.currentTarget) closeFocusThinking(); }}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 70,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'rgba(24, 40, 56, .26)',
        backdropFilter: 'blur(1px)',
      }}
    >
      <section
        aria-label="专注思考"
        style={{
          width: 'min(860px, 92vw)',
          height: 'min(88vh, 780px)',
          display: 'flex',
          flexDirection: 'column',
          background: '#fbfaf6',
          borderRadius: 14,
          border: '1px solid #dbe5ec',
          boxShadow: '0 30px 80px rgba(30, 50, 70, .35)',
          overflow: 'hidden',
        }}
      >
        <header
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '10px 16px',
            borderBottom: '1px solid #e2e9ee',
          }}
        >
          <strong style={{ fontSize: 13, color: '#2c4a63' }}>专注思考</strong>
          <button
            type="button"
            data-testid="focus-thinking-close"
            onClick={closeFocusThinking}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 12,
              padding: '5px 10px',
              border: '1px solid #cbdae4',
              borderRadius: 8,
              background: '#fffefa',
              color: '#3c5771',
              cursor: 'pointer',
            }}
          >
            <X size={13} />回到画布
          </button>
        </header>
        <div style={{ flex: 1, minHeight: 0, display: 'flex' }}>
          <ConversationPanel />
        </div>
      </section>
    </div>
  );
}
