'use client';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { createContext, useContext, useState, type PointerEvent as ReactPointerEvent } from 'react';
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
 * ## 输入草稿与回调为什么走 context,而不是 `data`
 *
 * React Flow 会在选中/布局变化时重建节点组件。如果 `selected` / `custom` 存在组件
 * 内部,一次重渲染就会把它们清空。所以草稿由 PathView 按 `questionId` 持有。
 *
 * 但它**不能塞进 `data`**:草稿每敲一个字都变,进了 `data` 就意味着整张节点数组
 * 重算 —— 业务节点也跟着换对象,而 React Flow 对"换了对象"的节点会重置测量,
 * 触发"测量 → 重渲染 → 再测量"的循环(用户看到的是节点持续闪烁)。放进 context
 * 之后,只有消费它的**问题节点**会重渲染,业务节点原样不动。
 */
export type CanvasQuestionDraft = {
  selected: string[];
  custom: string;
  error: string | null;
};

/** `data` 里只放**稳定的标识**:问题本身、是否主问题、是否聚焦。 */
export type CanvasQuestionData = {
  questionId: string;
  question: QuestionView;
  isPrimary: boolean;
  isFocused: boolean;
};

export type QuestionFlowNode = Node<CanvasQuestionData, 'question'>;

export const EMPTY_QUESTION_DRAFT: CanvasQuestionDraft = { selected: [], custom: '', error: null };

export type QuestionInteraction = {
  drafts: Record<string, CanvasQuestionDraft>;
  onDraftChange: (id: string, next: CanvasQuestionDraft) => void;
  onSubmit: (
    id: string,
    payload: { selectedOptionIds: string[]; customInput?: string | null },
  ) => Promise<boolean>;
  onSkip: (id: string) => Promise<boolean>;
  onLater: (id: string) => Promise<boolean>;
  onLocateSource: (sourceNodeId: string) => void;
};

const NOOP_INTERACTION: QuestionInteraction = {
  drafts: {},
  onDraftChange: () => undefined,
  onSubmit: async () => false,
  onSkip: async () => false,
  onLater: async () => false,
  onLocateSource: () => undefined,
};

export const QuestionInteractionContext = createContext<QuestionInteraction | null>(null);

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
  const interaction = useContext(QuestionInteractionContext) ?? NOOP_INTERACTION;
  const draft = interaction.drafts[question.id] ?? EMPTY_QUESTION_DRAFT;
  const [busy, setBusy] = useState(false);
  /** “更多”展开状态。纯 UI:默认只留问题 + 最多 3 个关键选项。 */
  const [expanded, setExpanded] = useState(false);

  const status = question.status;
  const processing = status === 'answered' || status === 'investigating';
  const resolved = status === 'resolved';
  const interactive = !processing && !resolved;
  const showOptions = question.responseMode !== 'free_text' && question.options.length > 0;
  const multiple = question.responseMode === 'multi_select';
  const showCustom = question.allowCustomInput || question.responseMode === 'free_text';
  const hasInput = draft.selected.length > 0 || draft.custom.trim().length > 0;

  function setDraft(next: CanvasQuestionDraft) {
    interaction.onDraftChange(question.id, next);
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
    const ok = await interaction.onSubmit(question.id, {
      selectedOptionIds: draft.selected,
      customInput: showCustom ? draft.custom.trim() || null : null,
    });
    setBusy(false);
    if (!ok) setDraft({ ...draft, error: '这次没有提交成功，你的回答还在，可以重试。' });
  }

  async function decide(action: 'skip' | 'later') {
    if (busy) return;
    setBusy(true);
    const ok =
      action === 'skip'
        ? await interaction.onSkip(question.id)
        : await interaction.onLater(question.id);
    setBusy(false);
    if (!ok) {
      setDraft({
        ...draft,
        error:
          action === 'skip' ? '跳过没有成功，可以再试一次。' : '稍后回答没有成功，可以再试一次。',
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
      {/*
        目标锚点。锚定虚线从这里进入卡片。**只在左侧留一个** —— 问题节点不是业务
        节点,不需要可连接的 source handle;这个 handle 只是给锚定边一个确定的落点
        (`isConnectable={false}` + 节点 `connectable:false` 一起保证它拖不出新边)。
      */}
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <p className="cq-question">{question.question}</p>

      {processing || resolved ? (
        <p className="cq-status" role="status">
          {STATUS_TEXT[status] ?? STATUS_LABEL[status]}
        </p>
      ) : (
        <>
          {showOptions && (
            <div className="cq-options">
              {question.options.slice(0, 3).map(option => {
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
          {!showOptions && showCustom && (
            <textarea
              className="cq-input nodrag"
              aria-label="补充你的回答"
              placeholder="写下你的回答…"
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
            <button
              type="button"
              className="cq-submit nodrag"
              disabled={busy || !hasInput}
              onPointerDown={press(() => void submit())}
            >
              {busy ? '提交中…' : draft.error ? '重试' : '提交回答'}
            </button>
            {/*
             * 次要与解释性内容全部收进“更多”。默认只留一句问题与最多 3 个选项 ——
             * 一张大号问卷卡会和路线/阶段争主画布。
             */}
            <button
              type="button"
              className="cq-more nodrag"
              aria-expanded={expanded}
              onPointerDown={press(() => setExpanded(value => !value))}
            >
              {expanded ? '收起' : '更多'}
            </button>
          </div>

          {expanded && (
            <div className="cq-details">
              <div className="cq-head">
                <span className="cq-badge">{STATUS_LABEL[status] ?? '待澄清'}</span>
                {data.isPrimary && <span className="cq-primary">最主要</span>}
              </div>
              {question.whyNow && <p className="cq-why">{question.whyNow}</p>}
              {question.sourceNodeId && (
                <button
                  type="button"
                  className="cq-source nodrag"
                  onPointerDown={press(() => interaction.onLocateSource(question.sourceNodeId as string))}
                >
                  查看来源
                </button>
              )}
              {showOptions && showCustom && (
                <textarea
                  className="cq-input nodrag"
                  aria-label="补充你的回答"
                  placeholder="也可以补充一句…"
                  value={draft.custom}
                  disabled={busy}
                  onPointerDown={event => event.stopPropagation()}
                  onChange={event => setDraft({ ...draft, custom: event.target.value, error: null })}
                />
              )}
              <div className="cq-actions">
                <button type="button" className="cq-text nodrag" disabled={busy} onPointerDown={press(() => void decide('later'))}>
                  稍后回答
                </button>
                <button type="button" className="cq-text nodrag" disabled={busy} onPointerDown={press(() => void decide('skip'))}>
                  跳过
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
