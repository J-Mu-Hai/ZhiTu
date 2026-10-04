'use client';
import { Handle, Position, type Node, type NodeProps } from '@xyflow/react';
import { createContext, useContext, useState, type PointerEvent as ReactPointerEvent } from 'react';
import type { QuestionView, V1CurrentInteraction, V1DimensionView } from '@/lib/backend';

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
  /**
   * 当前 active interaction 绑到这个节点时才存在。**它是节点唯一的交互来源** ——
   * 对话区不再渲染它的选项 / 输入框 / 确认按钮。
   */
  interaction?: V1CurrentInteraction | null;
  /** 这个节点是不是当前唯一 active 的那个(高亮用,不闪烁)。 */
  isActive?: boolean;
  /** V1 三阶段树的子问题从父阶段向下连接，而不是横向漂浮。 */
  verticalAnchor?: boolean;
  /**
   * 挂在这块基石上的内部维度(问题结构组的约束 / 杠杆 / 风险等)。
   * 它们不再单独成卡,而是在基石节点里以“关联”展示。
   */
  linkedDimensions?: V1DimensionView[];
  /** 目标定义节点:是否可以确认目标定义(无 interaction 的流程动作,也只在节点里)。 */
  goalConfirmable?: boolean;
  /** 继续形成战略路径(无 interaction 的流程动作)。 */
  continueStrategy?: boolean;
  /**
   * 合成节点:它没有真实的 `agent_questions` 行(战略 / 时间架构确认之类的待办)。
   * 这类节点上的自由文本要用聊天消息发出去,而不是去回答一个不存在的 question id。
   */
  synthetic?: boolean;
};

export type QuestionFlowNode = Node<CanvasQuestionData, 'question'>;

export const EMPTY_QUESTION_DRAFT: CanvasQuestionDraft = { selected: [], custom: '', error: null };

/**
 * V1 结构化动作。**只走画布节点**。
 *
 * 这些回调全部由 `PathView` 从 provider 里取最新闭包后包一层稳定身份传入,
 * 避免每次 provider 重渲染都换 context 身份(那是节点闪烁的机制之一)。
 */
export type V1NodeActions = {
  interaction: V1CurrentInteraction | null;
  /** 战略理解是否已确认 —— 决定 `strategy_review` 显示“确认理解”还是“确认战略”。 */
  strategyUnderstandingConfirmed: boolean;
  /** 目标定义节点上是否可以确认目标(无 interaction 的流程动作)。 */
  goalConfirmable: boolean;
  /** 是否处于“继续形成战略路径”这一步(无 interaction 的流程动作)。 */
  continueStrategy: boolean;
  /** 时间架构提案 id(`timeline_review` 用)。 */
  timelineProposalId: string | null;
  /** 已通过校验、等待确认的其它提案 id(`weekly_review` 用)。 */
  openProposalId: string | null;
  onSelectDirection: (key: string) => Promise<unknown> | void;
  onTimelineAlign: (options: { answer?: string; accepted?: boolean }) => Promise<unknown> | void;
  onAlignStrategy: () => Promise<unknown> | void;
  onConfirmStrategy: () => Promise<unknown> | void;
  onConfirmGoal: () => Promise<unknown> | void;
  onContinueStrategy: () => Promise<unknown> | void;
  onReopenDirection: () => Promise<unknown> | void;
  onConfirmProposal: (id: string) => Promise<unknown> | void;
  onSend: (text: string) => Promise<unknown> | void;
  /** 「做起来」阶段的三个生成入口。 */
  onRunPlanStep: (step: 'weekly' | 'daily' | 'review') => Promise<unknown> | void;
};

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
  v1: V1NodeActions;
};

const NOOP_INTERACTION: QuestionInteraction = {
  drafts: {},
  onDraftChange: () => undefined,
  onSubmit: async () => false,
  onSkip: async () => false,
  onLater: async () => false,
  onLocateSource: () => undefined,
  v1: {
    interaction: null,
    strategyUnderstandingConfirmed: false,
    goalConfirmable: false,
    continueStrategy: false,
    timelineProposalId: null,
    openProposalId: null,
    onSelectDirection: () => undefined,
    onTimelineAlign: () => undefined,
    onAlignStrategy: () => undefined,
    onConfirmStrategy: () => undefined,
    onConfirmGoal: () => undefined,
    onContinueStrategy: () => undefined,
    onReopenDirection: () => undefined,
    onConfirmProposal: () => undefined,
    onSend: () => undefined,
    onRunPlanStep: () => undefined,
  },
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

/**
 * 当前 active interaction 的**结构化控件**。
 *
 * 它是「画布节点是唯一结构化交互入口」的落点:关键问题、候选方向、节奏选择与
 * 节点确认都只在这里出现。对话区不再渲染这些控件。
 *
 * 数据全部来自 `interaction` 本身;关闭节点只收起视图,不丢 provider 里的状态。
 */
export function V1InteractionControls({ interaction, v1 }: { interaction: V1CurrentInteraction; v1: V1NodeActions }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const kind = interaction.kind;
  const recommended = interaction.recommendedOption;

  function press(action: () => Promise<unknown> | void) {
    return (event: ReactPointerEvent) => {
      event.stopPropagation();
      event.preventDefault();
      if (busy) return;
      setBusy(true);
      Promise.resolve(action()).finally(() => setBusy(false));
    };
  }

  return (
    <div className="cq-interaction" data-testid="cq-interaction" data-kind={kind}>
      {kind === 'candidate_selection' && (
        <div className="cq-options">
          {interaction.options.map(option => (
            <button
              key={option.key}
              type="button"
              disabled={busy}
              className={`cq-option cq-direction nodrag${option.key === recommended ? ' is-recommended' : ''}`}
              data-recommended={option.key === recommended ? 'true' : 'false'}
              onPointerDown={press(() => v1.onSelectDirection(option.key))}
            >
              {option.key === recommended && <span className="cq-recommended-tag">推荐</span>}
              <strong className="cq-option-title">{option.title}</strong>
              {option.reason && <span className="cq-option-reason">{option.reason}</span>}
              {option.impact && <em className="cq-option-impact">选择后果：{option.impact}</em>}
            </button>
          ))}
        </div>
      )}

      {kind === 'strategic_question' && (
        <div className="cq-answer">
          <textarea
            className="cq-input nodrag"
            aria-label="回答当前关键问题"
            placeholder="写下你的回答…"
            value={note}
            disabled={busy}
            onPointerDown={event => event.stopPropagation()}
            onChange={event => setNote(event.target.value)}
          />
          <button
            type="button"
            className="cq-submit nodrag"
            disabled={busy || !note.trim()}
            onPointerDown={press(() => { const value = note.trim(); setNote(''); return v1.onSend(value); })}
          >
            回答
          </button>
        </div>
      )}

      {kind === 'timeline_alignment' && (
        <>
          {interaction.options.length > 0 && (
            <div className="cq-options">
              {interaction.options.map(option => (
                <button
                  key={option.key}
                  type="button"
                  disabled={busy}
                  className="cq-option nodrag"
                  onPointerDown={press(() => v1.onTimelineAlign({ answer: option.title }))}
                >
                  {option.title}
                </button>
              ))}
            </div>
          )}
          <div className="cq-answer">
            <textarea
              className="cq-input nodrag"
              aria-label="调整时间节奏"
              placeholder="调整总周期 / 截止日期 / 不可用时间"
              value={note}
              disabled={busy}
              onPointerDown={event => event.stopPropagation()}
              onChange={event => setNote(event.target.value)}
            />
            <button
              type="button"
              className="cq-submit nodrag"
              disabled={busy || !note.trim()}
              onPointerDown={press(() => { const value = note.trim(); setNote(''); return v1.onTimelineAlign({ answer: value }); })}
            >
              提交调整
            </button>
          </div>
          <button
            type="button"
            className="cq-submit nodrag"
            disabled={busy}
            onPointerDown={press(() => v1.onTimelineAlign({ accepted: true }))}
          >
            认可默认节奏
          </button>
        </>
      )}

      {kind === 'strategy_review' && (
        <button
          type="button"
          className="cq-submit nodrag"
          disabled={busy}
          onPointerDown={press(() => (v1.strategyUnderstandingConfirmed ? v1.onConfirmStrategy() : v1.onAlignStrategy()))}
        >
          {v1.strategyUnderstandingConfirmed ? '确认战略' : '确认理解'}
        </button>
      )}

      {kind === 'timeline_review' && v1.timelineProposalId && (
        <button
          type="button"
          className="cq-submit nodrag"
          disabled={busy}
          onPointerDown={press(() => v1.onConfirmProposal(v1.timelineProposalId as string))}
        >
          确认时间架构
        </button>
      )}

      {kind === 'weekly_review' && v1.openProposalId && (
        <button
          type="button"
          className="cq-submit nodrag"
          disabled={busy}
          onPointerDown={press(() => v1.onConfirmProposal(v1.openProposalId as string))}
        >
          确认未来重规划
        </button>
      )}
    </div>
  );
}

export function CanvasQuestionNodeComponent({ data }: NodeProps<QuestionFlowNode>) {
  const question = data.question;
  const interaction = useContext(QuestionInteractionContext) ?? NOOP_INTERACTION;
  const v1 = interaction.v1;
  const draft = interaction.drafts[question.id] ?? EMPTY_QUESTION_DRAFT;
  const [busy, setBusy] = useState(false);
  /** “更多”展开状态。纯 UI:默认只留问题 + 最多 3 个关键选项。 */
  const [expanded, setExpanded] = useState(false);

  const status = question.status;
  const processing = status === 'answered' || status === 'investigating';
  const resolved = status === 'resolved';
  // P2.1:固定节点是**分析节点**,不是必答卡。**未被聚焦时不显示答题控件**;
  // 点开它才出现“补充 / 纠正”的输入。
  const interactive = !processing && !resolved && data.isFocused;
  const showOptions = question.responseMode !== 'free_text' && question.options.length > 0;
  const multiple = question.responseMode === 'multi_select';
  const showCustom = question.allowCustomInput || question.responseMode === 'free_text';
  const hasInput = draft.selected.length > 0 || draft.custom.trim().length > 0;
  /** 这个节点是不是当前唯一 active interaction 的落点。 */
  const nodeInteraction = data.interaction ?? null;
  const active = Boolean(data.isActive && nodeInteraction && nodeInteraction.status === 'active');
  /** active 且是「在对话框里回答」的关键问题 —— 仍走节点上的选项 / 自由回答。 */
  const questionAnswering = active && nodeInteraction?.kind === 'strategic_question';
  /** 无 interaction 的流程动作(确认目标 / 继续形成战略)也只在这里出现。 */
  const flowAction = Boolean(data.isActive && !active && (data.goalConfirmable || data.continueStrategy));
  /**
   * 阶段 10:有没有可信的战略判断。没有就诚实说“不足以推荐”,**不伪造**。
   * 旧行与部分对话路径的问题没有这些字段。
   */
  const hasJudgment = Boolean(
    question.analysisSummary.trim() || question.recommendation.trim() || question.decisionImpact.trim(),
  );
  /** 不足推荐时要说清缺什么。模型没给结构化字段时,用 `whyNow` 兜底。 */
  const missingStrategicInfo = question.whyNow.trim() || '目标的关键约束';
  /** 挂在这块基石上的内部维度(不再单独成卡)。 */
  const linked = data.linkedDimensions ?? [];

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
    // 合成节点没有真实 question id:自由文本作为一条普通消息发给 AI 解析。
    if (data.synthetic && v1.onSend) {
      const text = draft.custom.trim();
      if (!text) return;
      setBusy(true);
      setDraft({ ...draft, custom: '', error: null });
      await v1.onSend(text);
      setBusy(false);
      return;
    }
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

  /** 非 active 节点的自由补充:一句话交给 AI 去更新判断,不生成新的问卷。 */
  async function submitSupplement() {
    const text = draft.custom.trim();
    if (!text || busy) return;
    setBusy(true);
    setDraft({ ...draft, custom: '', error: null });
    await v1.onSend(text);
    setBusy(false);
  }

  /** 按钮统一走它:阻止画布选中/拖动,同时不让 click 在重渲染中丢失。 */
  function press(action: () => void) {
    return (event: ReactPointerEvent) => {
      event.stopPropagation();
      event.preventDefault();
      action();
    };
  }

  /*
   * 非 V1 问题仍走原来的「选项 + 提交」——它们没有 active interaction,
   * 画布节点本身就是唯一入口,不受 V1 的一条 active 约束影响。
   */
  const answerControls = (
            <>
              {showOptions && (
                <div className="cq-options">
                  {question.options.slice(0, 3).map(option => {
                    const isActive = draft.selected.includes(option.id);
                    return (
                      <button
                        type="button"
                        key={option.id}
                        className={`cq-option nodrag${isActive ? ' is-active' : ''}${option.recommended ? ' is-recommended' : ''}`}
                        aria-pressed={isActive}
                        disabled={busy}
                        data-recommended={option.recommended ? 'true' : 'false'}
                        onPointerDown={press(() => toggle(option.id))}
                      >
                        {option.recommended && <span className="cq-recommended-tag">推荐</span>}
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
              {hasJudgment && question.decisionImpact && (
                <p className="cq-impact">
                  <span className="cq-label">你的选择会影响</span>
                  {question.decisionImpact}
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
                  {question.confidenceNote && (
                    <p className="cq-assumption">仍需确认：{question.confidenceNote}</p>
                  )}
                  {question.sourceNodeId && (
                    <button
                      type="button"
                      className="cq-source nodrag"
                      onPointerDown={press(() => interaction.onLocateSource(question.sourceNodeId as string))}
                    >
                      查看来源
                    </button>
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
  );

  const classes = [
    'canvas-question-node',
    data.isPrimary ? 'is-primary' : '',
    data.isFocused ? 'is-focused' : '',
    processing ? 'is-processing' : '',
    resolved ? 'is-resolved' : '',
    data.isActive ? 'is-active' : '',
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
      <Handle type="target" position={data.verticalAnchor ? Position.Top : Position.Left} isConnectable={false} />
      {/*
        阶段 10:先展示 **AI 已经判断了什么**,再问“需要你确认的一点”。问题节点默认
        看得到判断、推荐与影响 —— 它不再像一张调查问卷。
      */}
      {/*
       * R2 收口:**节点卡默认只显示 标题 + 一句 AI judgment + 状态**。
       * 推荐、长问题文本、已知事实、选项都只在用户**主动进入节点**(聚焦/展开)后出现 ——
       * 默认画布不再是一张需要逐条回答的问卷,但仍然保留局部讨论能力。
       */}
      <div className="cq-judgment">
        <div className="cq-judgment-head">
          {question.v1Title ? <strong className="cq-v1-title">{question.v1Title}</strong> : null}
          <span className="cq-badge">{STATUS_LABEL[status] ?? '待澄清'}</span>
        </div>
        {hasJudgment ? (
          <p className="cq-analysis">{question.analysisSummary || question.recommendation}</p>
        ) : (
          <p className="cq-analysis cq-insufficient" data-testid="cq-insufficient">
            当前还不足以给出推荐。需要先确认这条战略信息：{missingStrategicInfo}。
          </p>
        )}
      </div>

      {/* 内部维度挂在这块基石上:紧凑时只显示数量,点开后列出它们。 */}
      {linked.length > 0 && !data.isFocused && (
        <p className="cq-linked-count" data-testid="cq-linked-count">关联讨论 {linked.length} 项</p>
      )}
      {linked.length > 0 && data.isFocused && (
        <div className="cq-linked" data-testid="cq-linked">
          <span className="cq-label">关联讨论</span>
          <ul>
            {linked.map((dimension) => (
              <li key={dimension.key}>
                <strong>{dimension.title}</strong>
                {dimension.judgment && <span className="cq-linked-judgment">{dimension.judgment}</span>}
                {((dimension.knownFacts?.length ?? 0) > 0 || (dimension.assumptions?.length ?? 0) > 0) && (
                  <em className="cq-linked-meta">
                    {(dimension.knownFacts?.length ?? 0) > 0 ? `事实 ${dimension.knownFacts!.length}` : ''}
                    {(dimension.knownFacts?.length ?? 0) > 0 && (dimension.assumptions?.length ?? 0) > 0 ? ' · ' : ''}
                    {(dimension.assumptions?.length ?? 0) > 0 ? `假设 ${dimension.assumptions!.length}` : ''}
                  </em>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {data.isFocused && question.recommendation && (
        <p className="cq-recommendation">
          <span className="cq-label">推荐</span>
          {question.recommendation}
        </p>
      )}

      {/* 规划智能体重构 V1(P2):已知事实与 AI 假设分开展示 —— 事实来自用户/系统,
          假设必须能被认出来是假设。 */}
      {question.v1Analysis && question.v1Analysis.knownFacts.length > 0 && data.isFocused && (
        <ul className="cq-known-facts">
          <li className="cq-label">已知事实</li>
          {question.v1Analysis.knownFacts.map((fact, index) => (
            <li key={index}>{fact}</li>
          ))}
        </ul>
      )}

      {/* 长问题文本只在用户主动进入节点、或该节点正在处理时出现。active 节点的问题
          文本由下面的交互区用 interaction.prompt 呈现,不重复。 */}
      {!active && (data.isFocused || expanded || processing) && (
        <>
          <span className="cq-label cq-label-question">需要你确认的一点</span>
          <p className="cq-question">{question.question}</p>
        </>
      )}

      {active && nodeInteraction ? (
        data.isFocused ? (
          questionAnswering ? (
            answerControls
          ) : (
            <>
              {nodeInteraction.whyNow && (
                <p className="cq-why"><span className="cq-label">为什么现在</span>{nodeInteraction.whyNow}</p>
              )}
              {nodeInteraction.context && (
                <p className="cq-context">{nodeInteraction.context}</p>
              )}
              <span className="cq-label cq-label-question">{nodeInteraction.title || '需要你确认的一点'}</span>
              <p className="cq-question">{nodeInteraction.prompt || question.question}</p>
              <V1InteractionControls interaction={nodeInteraction} v1={v1} />
            </>
          )
        ) : (
          <p className="cq-status cq-open-hint cq-active-hint">这是当前需要你确认的节点，点开继续。</p>
        )
      ) : flowAction ? (
        data.isFocused ? (
          <div className="cq-interaction" data-testid="cq-flow-action">
            {data.goalConfirmable && (
              <button
                type="button"
                className="cq-submit nodrag"
                disabled={busy}
                onPointerDown={press(() => { setBusy(true); Promise.resolve(v1.onConfirmGoal()).finally(() => setBusy(false)); })}
              >
                确认这个目标定义
              </button>
            )}
            {data.continueStrategy && (
              <button
                type="button"
                className="cq-submit nodrag"
                disabled={busy}
                onPointerDown={press(() => { setBusy(true); Promise.resolve(v1.onContinueStrategy()).finally(() => setBusy(false)); })}
              >
                继续形成战略路径
              </button>
            )}
          </div>
        ) : (
          <p className="cq-status cq-open-hint cq-active-hint">这一步需要你在节点里确认，点开继续。</p>
        )
      ) : processing || resolved ? (
        <p className="cq-status" role="status">
          {STATUS_TEXT[status] ?? STATUS_LABEL[status]}
        </p>
      ) : !data.isFocused ? (
        <p className="cq-status cq-open-hint">
          {question.v1Key != null
            ? '待讨论：点开后可以补充或纠正我的判断。'
            : '点开这个节点,可以补充或纠正我的判断。'}
        </p>
      ) : question.v1Key == null ? (
        answerControls
      ) : (
        <>
          {hasJudgment && question.decisionImpact && (
            <p className="cq-impact">
              <span className="cq-label">这个判断会影响</span>
              {question.decisionImpact}
            </p>
          )}
          <textarea
            className="cq-input nodrag"
            aria-label="补充或纠正"
            placeholder="补充一句，或纠正我的判断…"
            value={draft.custom}
            disabled={busy}
            onPointerDown={event => event.stopPropagation()}
            onChange={event => setDraft({ ...draft, custom: event.target.value, error: null })}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                void submitSupplement();
              }
            }}
          />
          {draft.error && (
            <p className="cq-error" role="alert">
              {draft.error}
            </p>
          )}
          <div className="cq-actions">
            <button
              type="button"
              className="cq-submit nodrag"
              disabled={busy || !draft.custom.trim()}
              onPointerDown={press(() => void submitSupplement())}
            >
              {busy ? '提交中…' : '提交补充'}
            </button>
            {question.sourceNodeId && (
              <button
                type="button"
                className="cq-text nodrag"
                onPointerDown={press(() => interaction.onLocateSource(question.sourceNodeId as string))}
              >
                查看来源
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
