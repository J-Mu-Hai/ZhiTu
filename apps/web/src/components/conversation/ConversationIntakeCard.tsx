'use client';
import { useState } from 'react';
import { useDemo } from '@/features/growth/provider';
import type { QuestionView } from '@/lib/backend';

/**
 * 战略澄清 intake 问题(阶段 11)。
 *
 * ## 为什么在对话区,而不是画布
 *
 * intake 阶段**还没有路线**,画布上也不该出现散乱讨论节点。所以关键问题只在对话区
 * 逐步出现,一次一个。它们是**唯一**能用橙色标记的提问 —— 橙色表达“战略校准中”,
 * 不滥用。
 *
 * ## 先判断,再提问
 *
 * 先把 AI 已经判断 / 推荐 / 选择影响摆出来,最后才是“需要你确认的一点”。
 * 没有可信判断时诚实说“当前还不足以给出推荐”,不编造。
 */
const LABEL = '战略校准';

export function ConversationIntakeCard({ question, index }: { question: QuestionView; index: number }) {
  const { submitAnswer, dismissQuestion, postponeQuestion } = useDemo();
  const [selected, setSelected] = useState<string[]>([]);
  const [custom, setCustom] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const multiple = question.responseMode === 'multi_select';
  const showOptions = question.responseMode !== 'free_text' && question.options.length > 0;
  const showCustom = question.allowCustomInput || question.responseMode === 'free_text';
  const hasJudgment = Boolean(
    question.analysisSummary.trim() || question.recommendation.trim() || question.decisionImpact.trim(),
  );
  const hasInput = selected.length > 0 || custom.trim().length > 0;

  function toggle(id: string) {
    setSelected(prev => (multiple ? (prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]) : [id]));
    setError(null);
  }

  async function submit() {
    if (busy || !hasInput) return;
    setBusy(true);
    const ok = await submitAnswer(question.id, {
      selectedOptionIds: selected,
      customInput: showCustom ? custom.trim() || null : null,
    });
    setBusy(false);
    if (!ok) setError('这次没有提交成功，你的回答还在，可以重试。');
  }

  async function decide(action: 'skip' | 'later') {
    if (busy) return;
    setBusy(true);
    const ok = action === 'skip' ? await dismissQuestion(question.id) : await postponeQuestion(question.id);
    setBusy(false);
    if (!ok) setError(action === 'skip' ? '跳过没有成功。' : '稍后回答没有成功。');
  }

  return (
    <div className="intake-card" role="group" aria-label={`战略校准问题：${question.question}`} data-testid="intake-card">
      <div className="intake-head">
        <span className="intake-pill">{LABEL}{index > 0 ? ` · 第 ${index} 个关键问题` : ' · 关键问题'}</span>
      </div>

      <div className="intake-judgment">
        <span className="intake-label">AI 判断</span>
        {hasJudgment ? (
          <>
            {question.analysisSummary && <p className="intake-analysis">{question.analysisSummary}</p>}
            {question.recommendation && (
              <p className="intake-recommendation">
                <span className="intake-label">推荐</span>
                {question.recommendation}
              </p>
            )}
          </>
        ) : (
          <p className="intake-analysis intake-insufficient">
            当前还不足以给出推荐。需要先确认这条战略信息：{question.whyNow || '目标的关键约束'}。
          </p>
        )}
      </div>

      <span className="intake-label">需要你确认的一点</span>
      <p className="intake-question">{question.question}</p>

      {showOptions && (
        <div className="intake-options">
          {question.options.slice(0, 3).map(option => {
            const active = selected.includes(option.id);
            return (
              <button
                type="button"
                key={option.id}
                className={`intake-option${active ? ' is-active' : ''}${option.recommended ? ' is-recommended' : ''}`}
                aria-pressed={active}
                disabled={busy}
                data-recommended={option.recommended ? 'true' : 'false'}
                onClick={() => toggle(option.id)}
              >
                {option.recommended && <span className="intake-rec-tag">推荐</span>}
                {option.label}
              </button>
            );
          })}
        </div>
      )}

      {showCustom && (
        <textarea
          className="intake-input"
          aria-label="补充你的回答"
          placeholder={showOptions ? '也可以补充一句…' : '写下你的回答…'}
          value={custom}
          disabled={busy}
          onChange={event => { setCustom(event.target.value); setError(null); }}
        />
      )}

      {hasJudgment && question.decisionImpact && (
        <p className="intake-impact">
          <span className="intake-label">你的选择会影响</span>
          {question.decisionImpact}
        </p>
      )}

      {error && <p className="intake-error" role="alert">{error}</p>}

      <div className="intake-actions">
        <button type="button" className="intake-submit" disabled={busy || !hasInput} onClick={() => void submit()}>
          {busy ? '提交中…' : error ? '重试' : '提交回答'}
        </button>
        <button type="button" className="intake-text" disabled={busy} onClick={() => void decide('later')}>稍后回答</button>
        <button type="button" className="intake-text" disabled={busy} onClick={() => void decide('skip')}>跳过</button>
      </div>
    </div>
  );
}
