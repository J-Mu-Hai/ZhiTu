'use client';
import { useState } from 'react';
import { useDemo } from '@/features/growth/provider';

/**
 * 输入框正上方的**当前关键行动**固定卡。
 *
 * 它不随消息滚动消失:用户永远不需要向上翻聊天记录去找“现在需要我回答什么”。
 * 数据只来自 `reasoning.v1CurrentInteraction`(服务端唯一投影),不自己拼状态。
 * 历史里的同一问题不再重复渲染完整选项(见 ConversationPanel)。
 */
export function CurrentInteractionCard() {
  const {
    reasoning,
    currentInteraction,
    sending,
    send,
    selectV1Direction,
    alignV1Strategy,
    alignV1Timeline,
    confirmV1Strategy,
    confirmRemote,
    remoteProposals,
    deciding,
  } = useDemo();
  const [text, setText] = useState('');

  if (!currentInteraction) return null;
  if (currentInteraction.status !== 'active') return null;

  const busy = sending || deciding;
  const kind = currentInteraction.kind;
  const openProposal = remoteProposals.find(
    proposal => proposal.status === 'validated' || proposal.status === 'pending_confirmation',
  );

  function submitText() {
    const value = text.trim();
    if (!value || busy) return;
    void send(value);
    setText('');
  }

  return (
    <section className="current-interaction-card" data-testid="current-interaction-card" data-kind={kind} aria-label="当前需要处理">
      <header className="cic-head">
        <span className="cic-tag">{currentInteraction.title}</span>
      </header>
      {currentInteraction.context && (
        <p className="cic-context"><strong>AI 判断：</strong>{currentInteraction.context}</p>
      )}
      {currentInteraction.whyNow && (
        <p className="cic-why"><strong>为什么现在：</strong>{currentInteraction.whyNow}</p>
      )}
      {currentInteraction.prompt && <p className="cic-prompt">{currentInteraction.prompt}</p>}

      {(kind === 'candidate_selection' || kind === 'timeline_alignment') && currentInteraction.options.length > 0 && (
        <div className="cic-options">
          {currentInteraction.options.map(option => (
            <button
              key={option.key}
              type="button"
              disabled={busy}
              className={option.key === currentInteraction.recommendedOption ? 'is-recommended' : ''}
              onClick={() => {
                if (kind === 'candidate_selection') void selectV1Direction(option.key);
                else void alignV1Timeline({ answer: option.title });
              }}
            >
              <strong>{option.title}</strong>
              {option.reason && <span>{option.reason}</span>}
              {option.impact && <em>{option.impact}</em>}
              {option.key === currentInteraction.recommendedOption && <i className="cic-recommended">推荐</i>}
            </button>
          ))}
        </div>
      )}

      {kind === 'timeline_alignment' && (
        <button type="button" className="cic-primary" disabled={busy} onClick={() => void alignV1Timeline({ accepted: true })}>
          认可默认节奏
        </button>
      )}

      {kind === 'strategy_review' && (
        <div className="cic-actions">
          {reasoning?.v1StrategyUnderstanding && !reasoning.v1StrategyUnderstanding.confirmed ? (
            <button type="button" className="cic-primary" disabled={busy} onClick={() => void alignV1Strategy()}>
              确认理解
            </button>
          ) : (
            <button type="button" className="cic-primary" disabled={busy} onClick={() => void confirmV1Strategy()}>
              确认战略
            </button>
          )}
        </div>
      )}

      {kind === 'timeline_review' && reasoning?.v01TimelineProposalId && (
        <button
          type="button"
          className="cic-primary"
          disabled={busy}
          onClick={() => void confirmRemote(reasoning.v01TimelineProposalId as string)}
        >
          确认时间架构
        </button>
      )}

      {kind === 'weekly_review' && openProposal && (
        <button type="button" className="cic-primary" disabled={busy} onClick={() => void confirmRemote(openProposal.id)}>
          确认未来重规划
        </button>
      )}

      {kind === 'strategic_question' && (
        <div className="cic-answer">
          <textarea
            aria-label="回答当前关键问题"
            value={text}
            disabled={busy}
            placeholder="写下你的回答…"
            onChange={event => setText(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submitText();
              }
            }}
          />
          <button type="button" className="cic-primary" disabled={busy || !text.trim()} onClick={submitText}>
            {busy ? '提交中…' : '回答'}
          </button>
        </div>
      )}
    </section>
  );
}
