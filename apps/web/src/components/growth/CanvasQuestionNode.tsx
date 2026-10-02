'use client';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { useState, type PointerEvent as ReactPointerEvent } from 'react';
import type { QuestionView } from '@/lib/backend';

/**
 * 画布问题节点。
 *
 * ## 它是什么 / 不是什么
 *
 * 它是 `agent_questions` 里一条待回答问题在**画布上的投影**,不是知识节点:
 * 不是 `plan_nodes` 的一行,不参与排期、任务统计、依赖图或正式计划树,也不写进
 * 任何关系表。它只在被读取到的那一份 `questions` 上存在,刷新后从 `GET /questions`
 * 重新投影出来("仅靠内存状态"是不允许的)。
 *
 * ## 为什么交互走 `onPointerDown` 而不是 `onClick`
 *
 * React Flow 在节点上监听指针事件来做选中/拖动。落在节点内部的 `onClick` 会因为
 * 中途的重渲染而丢失(实测:点了选项、按钮却一直是灰的)。所以按钮动作直接挂在
 * `onPointerDown` 上,并 `stopPropagation` —— 既不触发画布选中,也不会丢事件。
 *
 * ## 输入草稿为什么由外部持有
 *
 * React Flow 在选中/布局变化时会重建节点组件。如果 `selected` / `custom` 存在组件
 * 内部,一次重渲染就会把它们清空。所以草稿(`draft`)由 PathView 按 `questionId` 持有。
 */
export type CanvasQuestionDraft = {
  selected: string[];
  custom: string;
  error: string | null;
};

export type CanvasQuestionData = {
  questionId: string;
  question: QuestionView;
  isPrimary: boolean;
  isFocused: boolean;
  draft: CanvasQuestionDraft;
  onDraftChange: (id: string, next: CanvasQuestionDraft) => void;
  onSubmit: (
    id: string,
    payload: { selectedOptionIds: string[]; customInput?: string | null },
  ) => Promise<boolean>;
  onSkip: (id: string) => Promise<boolean>;
  onLater: (id: string) => Promise<boolean>;
  onLocateSource: (sourceNodeId: string) => void;
};

export type QuestionFlowNode = Node<CanvasQuestionData, 'question'>;

export const EMPTY_QUESTION_DRAFT: CanvasQuestionDraft = { selected: [], custom: '', error: null };

const STATUS_LABEL: Record<string, string> = {
  pending: '待澄清',
  answered: '已收到回答',
  investigating: '正在处理',
  resolved: '已澄清',
  archived: '已归档',
};

const STATUS_TEXT: Record<string, string> = {
  answered: '已收到你的回答，正在处理…',
  investigating: '正在处理你的回答…',
  resolved: '这个问题已经澄清了。',
};

export function CanvasQuestionNodeComponent({ data }: NodeProps<QuestionFlowNode>) {
  const question = data.question;
  const draft = data.draft;
  const [busy, setBusy] = useState(false);

  const status = question.status;
  const processing = status === 'answered' || status === 'investigating';
  const resolved = status === 'resolved';
  const interactive = !processing && !resolved;
  const showOptions = question.responseMode !== 'free_text' && question.options.length > 0;
  const multiple = question.responseMode === 'multi_select';
  const showCustom = question.allowCustomInput || question.responseMode === 'free_text';
  const hasInput = draft.selected.length > 0 || draft.custom.trim().length > 0;

  function setDraft(next: CanvasQuestionDraft) {
    data.onDraftChange(question.id, next);
  }

  function toggle(optionId: string) {
    const selected = multiple
      ? draft.selected.includes(optionId)
        ? draft.selected.filter(id => id !== optionId)
        : [...draft.selected, optionId]
      : [optionId];
    setDraft({ ...draft, selected, error: null });
  }

  async function submit() {
    if (!interactive || busy || !hasInput) return;
    setBusy(true);
    const ok = await data.onSubmit(question.id, {
      selectedOptionIds: draft.selected,
      customInput: showCustom ? draft.custom.trim() || null : null,
    });
    setBusy(false);
    if (!ok) setDraft({ ...draft, error: '这次没有提交成功，你的回答还在，可以重试。' });
  }

  async function decide(action: 'skip' | 'later') {
    if (busy) return;
    setBusy(true);
    const ok = action === 'skip' ? await data.onSkip(question.id) : await data.onLater(question.id);
    setBusy(false);
    if (!ok) {
      setDraft({
        ...draft,
        error: action === 'skip' ? '跳过没有成功，可以再试一次。' : '稍后回答没有成功，可以再试一次。',
      });
    }
  }

  /** 按钮统一走它:阻止画布选中/拖动,同时不让 click 在重渲染中丢失。 */
  function press(action: () => void) {
    return (event: ReactPointerEvent) => {
      event.stopPropagation();
      event.preventDefault();
      action();
    };
  }

  const classes = [
    'canvas-question-node',
    data.isPrimary ? 'is-primary' : '',
    data.isFocused ? 'is-focused' : '',
    processing ? 'is-processing' : '',
    resolved ? 'is-resolved' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={classes} role="group" aria-label={`待澄清问题：${question.question}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="cq-head">
        <span className="cq-badge">{STATUS_LABEL[status] ?? '待澄清'}</span>
        {data.isPrimary && <span className="cq-primary">最主要</span>}
      </div>
      <p className="cq-question">{question.question}</p>
      {question.whyNow && !processing && !resolved && <p className="cq-why">{question.whyNow}</p>}
      {question.sourceNodeId && (
        <button
          type="button"
          className="cq-source nodrag"
          onPointerDown={press(() => data.onLocateSource(question.sourceNodeId as string))}
        >
          查看来源
        </button>
      )}

      {processing || resolved ? (
        <p className="cq-status" role="status">
          {STATUS_TEXT[status] ?? STATUS_LABEL[status]}
        </p>
      ) : (
        <>
          {showOptions && (
            <div className="cq-options">
              {question.options.map(option => {
                const active = draft.selected.includes(option.id);
                return (
                  <button
                    type="button"
                    key={option.id}
                    className={`cq-option nodrag${active ? ' is-active' : ''}`}
                    aria-pressed={active}
                    disabled={busy}
                    onPointerDown={press(() => toggle(option.id))}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
          )}
          {showCustom && (
            <textarea
              className="cq-input nodrag"
              aria-label="补充你的回答"
              placeholder={showOptions ? '也可以补充一句…' : '写下你的回答…'}
              value={draft.custom}
              disabled={busy}
              onPointerDown={event => event.stopPropagation()}
              onChange={event => setDraft({ ...draft, custom: event.target.value, error: null })}
              onKeyDown={event => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  void submit();
                }
              }}
            />
          )}
          {draft.error && (
            <p className="cq-error" role="alert">
              {draft.error}
            </p>
          )}
          <div className="cq-actions">
            <button type="button" className="cq-text nodrag" disabled={busy} onPointerDown={press(() => void decide('later'))}>
              稍后回答
            </button>
            <button type="button" className="cq-text nodrag" disabled={busy} onPointerDown={press(() => void decide('skip'))}>
              跳过
            </button>
            <button
              type="button"
              className="cq-submit nodrag"
              disabled={busy || !hasInput}
              onPointerDown={press(() => void submit())}
            >
              {busy ? '提交中…' : draft.error ? '重试' : '提交回答'}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
