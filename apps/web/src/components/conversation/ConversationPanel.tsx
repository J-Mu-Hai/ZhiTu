'use client';
import { BrandMark } from '@/components/ui/BrandMark';
import { useEffect, useRef, useState, useCallback, useSyncExternalStore } from 'react';
import { ArrowUp, Plus, X, CornerDownLeft, AlertCircle, RotateCcw, RefreshCw } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import { readConversationDraft, subscribeConversationDraft, writeConversationDraft } from '@/features/growth/drafts';
import { V1_READY_PROMPT } from '@/features/growth/v1Workflow';
import { V1_CORE_GOAL_KEYS } from '@/features/growth/v1Analysis';
import { degradedHint, sourceLabel } from '@/lib/backend';
import type { ResearchView } from '@/lib/backend';

/**
 * 提案落下之后,卡片上显示的状态。
 *
 * 不能直接印 `remote.status` —— `pending_confirmation` 不是给人看的词。而且
 * **每一个状态都要有一句话**,包括 `failed`:`?? remote.status` 会在界面上留下
 * 一个英文枚举值,而用户没法判断那到底是"成了"还是"没成"。
 */
const PROPOSAL_STATUS_LABEL: Record<string, string> = {
  applied: '✓ 已写入计划',
  rejected: '已放弃这次变更，计划未改动',
  stale: '计划已经变了，这份提议作废',
  failed: '写入没有成功，计划未改动',
};

/**
 * 没有拿到来源时,按 `status` 说清楚"这一轮公开研究发生了什么"。
 *
 * **不能沉默。** 沉默会让用户以为"它查过了、就是没找到",而事实可能是"根本没
 * 启用"或"查询里带了私密信息被拦下"—— 这两件事对用户的意义完全不同。
 */
const RESEARCH_STATUS_LABEL: Record<string, string> = {
  unavailable: '这一轮没有联网查询：公开研究未启用。',
  blocked: '这个查询包含私密信息，服务端已拦下，没有发到公网。',
  limited: '今天的公开研究额度已用完，这一轮没有联网查询。',
  timeout: '公开研究超时，这次没有拿到来源。',
  failed: '公开研究没有完成，这次没有拿到来源。',
};

/**
 * 一条助手回复所依据的公开来源。
 *
 * ## 为什么它能被信任
 *
 * 这些引用**不是模型写的** —— 每一项都来自服务端真实执行的 `research_public`
 * (见 `ResearchCitationView` 的注释)。所以它和下面的提案卡一样,是可以点开核对
 * 的证据,而不是模型的一段转述。`status` 不是 `success` / `cached` 时如实说没有
 * 拿到来源,绝不假装查过。
 */
function ResearchCitations({ research }: { research: ResearchView }) {
  const cited = research.status === 'success' || research.status === 'cached';
  if (cited && research.citations.length > 0) {
    return (
      <div className="research-citations">
        <span className="eyebrow">
          {research.status === 'cached' ? '参考的公开来源（缓存）' : '参考的公开来源'}
        </span>
        <ul>
          {research.citations.map(citation => (
            <li key={citation.sourceId}>
              {/* `noreferrer` 不只是礼节:来源 URL 可能带查询参数,不该把当前页面
                  的地址作为 Referer 送给第三方站点。 */}
              <a href={citation.url} target="_blank" rel="noopener noreferrer">
                {citation.title}
              </a>
              <span className="research-domain">{citation.domain}</span>
              {citation.excerpt && <em className="research-excerpt">{citation.excerpt}</em>}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  // 没有来源但确实尝试过:如实地说明这一轮公开研究的状态。
  if (!research.consulted) return null;
  return (
    <p className="research-note" role="status">
      {RESEARCH_STATUS_LABEL[research.status ?? ''] ?? '公开研究没有完成，这次没有拿到来源。'}
    </p>
  );
}

/**
 * 一个待回答的问题的**紧凑**状态。
 *
 * ## 为什么不再是正文里的大卡片
 *
 * 旧版把整个问题正文、来源标题与一个定位按钮铺在对话正文/输入区上方 —— 它和画布
 * 上的 Question Node 是同一件事说两遍,而且占掉了正文空间。这一版只在标题栏下方留
 * 一行:`待回答问题 N · 定位到画布`。
 *
 * **完整回答只发生在画布的 Question Node 里** —— 这里不提供第二份可提交控件,
 * 只负责定位。它高度不到 32px,不展示正文/理由/来源/选项。
 */
function QuestionStatusBar({
  count,
  processing,
  onLocate,
}: {
  count: number;
  processing: boolean;
  onLocate: () => void;
}) {
  return (
    <div className="question-status-bar" role="status" aria-label={`画布上有 ${count} 个待回答问题`}>
      <span className="tiny-dot" aria-hidden="true" />
      <span className="question-status-text">{processing ? '正在处理你的回答' : `待回答问题 ${count}`}</span>
      <button type="button" className="question-locate" onClick={onLocate}>
        定位到画布
      </button>
    </div>
  );
}

/**
 * 提案校验失败的**紧凑**提示。
 *
 * ## 为什么不是一条长错误
 *
 * "AI 说改了、其实没写进去"是一个重要事实,不能被吞掉 —— 但旧版把后端返回的
 * 全部校验原因一次性铺出来,长的时候会把下面的消息推得很远,用户第一眼看到的
 * 是"系统报错了",而不是"这次建议没生效,计划没变"。
 *
 * 所以默认只留一行结论(`本次计划建议未应用`);原始原因要点"查看原因"才展开。
 *
 * ## 关闭只是关闭这条前端提示
 *
 * 关闭状态是这个组件自己的 `useState`,不碰 provider 里的 `proposalErrors`,
 * 也不碰任何提案状态 —— 关掉它不会把一次失败伪装成成功。父级用错误的
 * `code+message` 当 `key`,来了**新**的错误会重新挂载,旧的关闭状态不会把它一起藏掉。
 */
function ProposalErrorNotice({ errors }: { errors: { code: string; message: string }[] }) {
  const [open, setOpen] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  if (dismissed) return null;
  // 同一个原因可能出现在多条动作上,去重后再显示。
  const reasons = Array.from(new Set(errors.map(error => error.message)));
  return (
    <div className="turn-error proposal-error" role="alert">
      <div className="turn-error-line">
        <AlertCircle size={14} />
        <span>本次计划建议未应用</span>
        <button type="button" className="turn-error-toggle" aria-expanded={open} onClick={() => setOpen(value => !value)}>
          查看原因
        </button>
        <button type="button" className="turn-error-dismiss" aria-label="关闭这条提示" onClick={() => setDismissed(true)}>
          <X size={13} />
        </button>
      </div>
      {open && (
        <ul className="turn-error-reasons">
          {reasons.map((reason, index) => <li key={index}>{reason}</li>)}
        </ul>
      )}
    </div>
  );
}

/**
 * intake 关键问题下面的**轻量快捷回复 chips**。
 *
 * ## 它为什么不是一张卡片
 *
 * 快捷回复是加速器,不是限制。它看起来就该是 AI 那条消息下方的一排可点的短句,
 * 点一下等价于把这句话作为回答发出去;用户也永远可以直接在输入框里自由作答。
 *
 * 它们**没有“推荐 / 分析 / 更多”这类问卷标签**,也**不是后端问题实体** —— 只是
 * 模型这一轮给出的 0–3 句可点的话。
 */
function IntakeChips({
  replies,
  disabled,
  onSend,
}: {
  replies: string[];
  disabled: boolean;
  onSend: (text: string) => void;
}) {
  if (replies.length === 0) return null;
  return (
    <div className="intake-chips" data-testid="intake-chips">
      {replies.map(reply => (
        <button
          key={reply}
          type="button"
          className="intake-chip"
          disabled={disabled}
          onClick={() => onSend(reply)}
        >
          {reply}
        </button>
      ))}
    </div>
  );
}

/**
 * 与 AI 的对话面板。
 *
 * ## 这一版为什么长这样
 *
 * 上一版没有"失败"这个状态。`send()` 在请求失败后吞掉异常、编一条中文回复,
 * 界面上看起来和真模型没有区别。用户没有任何办法知道那句话不是 AI 说的。
 *
 * 所以这里有四件以前没有的事:
 *
 * 1. **每条助手消息带来源徽标**。"AI 规划 · DeepSeek" / "本地规则 · 模型不可用"。
 *    历史消息也带 —— 往上翻的时候同样看得出来。
 * 2. **降级时说明原因**(额度用尽?超时?密钥无效?),而不是只给一个通用错误。
 * 3. **失败就是失败**:错误行 + 重试按钮。`retryable=false` 时按钮灰掉 ——
 *    让用户点一个注定失败的按钮比不给他按钮更糟。proposal 校验失败则收成
 *    一行可展开、可关闭的提示(见 `ProposalErrorNotice`)。
 *
 * ## 这一版收掉了旧版的两条常驻诊断
 *
 * `brief-missing`("还缺:截止时间、每周投入……")与 `strategy-hint`("还没有已确认
 * 的战略方向……")不再渲染。右侧是对话与输入区,不是系统诊断面板;战略阶段也不该
 * 被"截止时间/每周投入"干扰。`replan` 入口只在**已确认战略 + 存在执行计划**时才出现。
 */
export function ConversationPanel() {
  const { growth, selectedId, select, messages, remoteProposals, proposalErrors, inputChanged, deciding, confirmRemote, rejectRemote, replan, replanState, send, retry, sending, sendError, retryable, historyLoading, messagesTruncated, spaceId, workspaceId, questions, focusQuestion, openTrace, traceAvailability, reasoning, agentStatus,
  currentInteraction } = useDemo();
  /*
   * 草稿住在组件外面(见 `features/growth/drafts.ts`)。
   *
   * 键用**工作区 id**,不是 `spaceId` —— 后者在计划还没加载时会先落在哨兵值 `'goal'`
   * 上,加载完之后才换成真实节点 id。用它做键的话,用户在加载那一瞬间打的字会
   * 被当成另一份草稿丢掉(实测:发送按钮一直是灰的)。工作区 id 从一开始就是稳定的。
   */
  const draftKey = workspaceId;
  const input = useSyncExternalStore(
    useCallback((onChange: () => void) => subscribeConversationDraft(draftKey, onChange), [draftKey]),
    () => readConversationDraft(draftKey),
    () => '',
  );
  const setInput = useCallback((value: string) => writeConversationDraft(draftKey, value), [draftKey]);
  const [showContexts, setShowContexts] = useState(false);
  /** 输入框的 DOM 元素。高度按内容算(见下面那个 effect)。 */
  const composer = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const history = useRef<HTMLDivElement>(null);
  const selected = selectedId ? growth.nodes[selectedId] : null;

  /**
   * 还没被决定、也没有哪条消息指向它的提案。
   *
   * 「没有消息指向」是关键条件,它挡住的是重复渲染:正常一轮对话产生的提案就
   * 挂在那条助手回复上(服务端把两者写在一起),再在末尾重复一遍是同一件事
   * 说两遍。
   *
   * 那么什么时候会真的出现"没有消息指向的提案"?**当那条消息不在这个列表里的时候。**
   * 后端每次只回最近的一批消息(`truncated` 为真就说明有更早的没回来),而提案是
   * 挂在消息上的 —— 一条两周前 AI 提过、用户一直没处理的调整,它的消息早就落在
   * 窗口之外了。这份渲染兜住的就是它:一份**还没被决定**的提案不应该因为用户
   * 聊了太多天就变得看不见、点不着。
   */
  const orphanProposals = remoteProposals.filter(
    proposal => proposal.status === 'validated' || proposal.status === 'pending_confirmation',
  ).filter(proposal => !messages.some(message => message.proposalId === proposal.id));

  /**
   * 同一时刻只突出**一个**主问题。
   *
   * 优先一个还没答的;没有的话,正在处理的也要显示(用户刚答完,不能让它凭空消失,
   * 否则他会以为答案没提交上)。列表已由后端按时间排好,这里只做挑选,不再排序。
   */
  /**
   * 只有 `canvas_question` 才参与“待回答问题 / 定位到画布”。
   *
   * 阶段 12 起战略 intake **不再落问题实体**;但老数据里可能残留
   * `conversation_intake` 行。它们必须在这里就被排除,不能重新变成 primaryQuestion、
   * 不能显示定位按钮、不能劫持用户输入。
   */
  const canvasQuestions = questions.filter(
    question => question.presentation !== 'conversation_intake',
  );
  const primaryQuestion =
    canvasQuestions.find(question => question.status === 'pending') ??
    canvasQuestions.find(question => question.status === 'investigating' || question.status === 'answered') ??
    null;
  /**
   * 阶段 12:战略澄清 intake 的问题**就在对话流里**,不是一张常驻表单。
   *
   * - `intakeActive` 决定要不要显示极轻量的进度文字“正在梳理目标 · 2/5”;
   * - `primaryIntake` 来自会话状态 `reasoning.pendingIntake`(服务端一次只给一个);
   * - 它**挂在产生它的那条助手消息下面** —— 判断在上、问题与快捷回复紧随其后。
   */
  const intakeActive = reasoning?.phase === 'intake';
  /**
   * 规划智能体重构 V1(P1):阶段一「初步思考」。
   *
   * 放大输入框**在右侧面板内**,不覆盖画布;提交后 `v1Stage` 变化,这一档自动退出,
   * 面板回到普通尺寸与普通对话布局(节点局部讨论就用普通面板 + 画布详情卡)。
   */
  const v1Initial = reasoning?.v1Stage === 'initial_thinking';
  /** 规划智能体重构 V1(P2.1):是不是 V1 会话。 */
  const isV1 = Boolean(reasoning?.v1Stage);
  /*
   * 对话区**不再渲染战略仪表盘**:“我的理解 / 当前战略判断 / 已采用的起点 /
   * 战略路径草案”都已删除。保留的只有失败状态与简短等待提示。
   */
  const v1StatusFailed = reasoning?.v1Status === 'failed';
  // 阶段 12:待回答问题只来自会话状态(`reasoning.pendingIntake`),**不是问题实体**。
  const primaryIntake = reasoning?.pendingIntake ?? null;
  const intakeAsked = reasoning?.intakeQuestionsAsked ?? 0;
  const intakeLimit = reasoning?.intakeQuestionLimit ?? 5;
  const lastAssistantId = [...messages].reverse().find(message => message.role === 'assistant')?.id ?? null;
  // 服务端给的消息 id 可能落在窗口外 —— 那样就退回最后一条助手消息,
  // 绝不让“当前最该回答的问题”因为消息分页而消失。
  const intakeMessageId = primaryIntake
    ? (primaryIntake.messageId && messages.some(message => message.id === primaryIntake.messageId)
        ? primaryIntake.messageId
        : lastAssistantId)
    : null;
  const showLive = agentStatus !== null && (agentStatus.tone === 'starting' || agentStatus.tone === 'running' || agentStatus.tone === 'waiting_long');
  /** 这个空间里有没有**已确认的**战略节点。没有时,周/日安排没有依据。 */
  const hasStrategy = Object.values(growth.nodes).some(
    node => node.planningLevel === 'strategy',
  );
  /**
   * 有没有**可调整的执行计划** —— 阶段/月/周/日这一层。
   *
   * `replan` 调的是"按执行偏差重排",没有这一层就没有可调整的对象。所以入口的
   * 显示条件是**这两件事同时成立**;缺任何一个,它整块不渲染,不占对话区一行。
   */
  const hasExecutionPlan = Object.values(growth.nodes).some(
    node => node.planningLevel === 'phase'
      || node.planningLevel === 'month'
      || node.planningLevel === 'week'
      || node.planningLevel === 'day',
  );
  const canReplan = hasStrategy && hasExecutionPlan;

  // 提案也要跟着滚。`replan` 产出的那份提案是**追加在末尾**的,不滚过去的话
  // 用户点了「按执行情况调整」会看到界面毫无反应。
  // 只在用户位于底部附近时才自动滚到底;正在查看历史时不得强行跳动。
  useEffect(() => {
    const el = history.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom) bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages.length, sending, remoteProposals.length]);

  /**
   * 输入框随内容长高,到 120px 就内部滚动。
   *
   * 先置 `auto` 再读 `scrollHeight`:不置 auto 的话读到的是**当前**高度,删字时
   * 它就永远缩不回去。上下限由 CSS 的 `min-height`/`max-height` 兜底(48/120),
   * 所以这里不需要自己夹紧。
   */
  useEffect(() => {
    const el = composer.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, [input]);

  function submit() {
    if (!input.trim() || sending) return;
    void send(input.trim());
    setInput('');
  }

  /*
   * 当前唯一待处理动作在**画布节点**里的落点。
   *
   * 对话区只说明“这个决定放在哪个节点、确认后会怎样”,并给一个「定位到节点」。
   * 选项、输入框与确认按钮一律不在对话区出现 —— 那是画布节点的职责。
   */
  const interactionQuestion = currentInteraction?.focusKey && V1_CORE_GOAL_KEYS.includes(currentInteraction.focusKey)
    ? questions.find(question => question.v1Key === currentInteraction.focusKey) ?? null
    : null;
  /*
   * 没有 interaction 的流程动作(确认目标定义 / 继续形成战略路径)也把决定放在
   * 画布节点里。对话区对它们同样只给一句位置说明 + 定位。
   */
  const goalConfirmable =
    reasoning?.v1Stage === 'goal_reframe' && Boolean(reasoning?.v1StrategicThesis);
  const continueStrategy = reasoning?.v1NextAction === 'continue_strategy';
  const activeInteraction = currentInteraction?.status === 'active' ? currentInteraction : null;
  /*
   * review 类动作绑在**阶段节点**上(战略 → 想清楚,时间 → 排出来,周回顾 → 做起来);
   * 问答类动作绑在对应的分析子节点上。对话区只给一个「定位到节点」。
   */
  const reviewPhase: { key: 'think' | 'plan' | 'do'; title: string } | null = activeInteraction
    ? activeInteraction.kind === 'strategy_review'
      ? { key: 'think', title: '想清楚' }
      : activeInteraction.kind === 'timeline_alignment' || activeInteraction.kind === 'timeline_review'
        ? { key: 'plan', title: '排出来' }
        : activeInteraction.kind === 'weekly_review'
          ? { key: 'do', title: '做起来' }
          : null
    : null;
  /*
   * 有 active interaction 时以它为准(阶段/子节点);没有时再看“确认目标 / 继续战略”
   * 这类流程动作。**不能让 goalConfirmable 盖过交互本身** —— 否则一个
   * `strategic_question` 会被写成“想清楚”。
   */
  const noticeTarget = activeInteraction
    ? reviewPhase
      ? `v1phase:${reviewPhase.key}`
      : interactionQuestion?.id ?? 'v1phase:think'
    : goalConfirmable || continueStrategy
      ? 'v1phase:think'
      : null;
  const noticeTitle = activeInteraction
    ? reviewPhase
      ? reviewPhase.title
      : interactionQuestion?.v1Title ?? activeInteraction.title
    : goalConfirmable || continueStrategy
      ? '想清楚'
      : '画布';
  const noticeImpact = activeInteraction?.kind === 'strategy_review'
    ? '收束战略'
    : activeInteraction?.kind === 'timeline_review'
      ? '生成时间架构'
      : activeInteraction?.kind === 'timeline_alignment'
        ? '对齐时间节奏'
        : activeInteraction?.kind === 'candidate_selection'
          ? '确定起点'
          : goalConfirmable
            ? '进入问题结构'
            : continueStrategy
              ? '继续形成战略路径'
              : '继续推进';
  const noticeVisible = Boolean(isV1 && (activeInteraction || goalConfirmable || continueStrategy));

  return (
    <aside className="conversation-panel" aria-label="与 AI 一起思考">
      <header className="conversation-header">
        <div className="ai-symbol"><BrandMark size={28} /></div>
        <div>
          <h2>与 AI 一起思考</h2>
          <p>基于当前空间 · {growth.nodes[spaceId]?.title ?? growth.title}</p>
        </div>
        {/* 「示例空间」这个标签没有了 —— 因为它指的那个东西没有了。
            留在这里最坏的情况是它**永远不显示**,而那种"看不出来坏了"的控件
            比明着报错更难发现。 */}
        {/*
          等待回复的状态标识**不在这里**。

          它原来放在这个 `<header>` 里,而 `.floating-conversation .conversation-header`
          在 `demo2.css` 里是 `display:none` —— 也就是说,在这个面板**唯一被渲染的地方**
          (工作台那个浮动 Dock),它一次都不会显示。这不是"位置不够好",是一条
          永远不执行的路径:看起来做了,实际什么都没有发生。

          所以它挪到了 `.floating-title` —— Dock 上真正看得见的那一行(见
          `FloatingConversation`)。这里只留一句说明,免得下一个人又把它加回来。
        */}
      </header>

      {/*
       * **待回答问题:标题栏下方一条紧凑状态。**
       *
       * 它取代了正文里那张大卡片 —— 同一件事只在一处说,而且不占正文空间。
       * 只在存在活动问题时出现;回答仍然只在画布的 Question Node 里完成。
       */}
      {!isV1 && primaryQuestion && primaryQuestion.presentation !== 'conversation_intake' && (
        <QuestionStatusBar
          count={questions.length}
          processing={primaryQuestion.status === 'answered' || primaryQuestion.status === 'investigating'}
          onLocate={() => focusQuestion(primaryQuestion.id)}
        />
      )}

      {/*
       * **当前决定放在哪个画布节点。**
       *
       * 对话区只说明位置并给「定位到节点」——选项、输入框与确认按钮都在画布节点里
       * (见 `CanvasQuestionNode`)。它固定在历史之上,不需要滚动就能看到。
       */}
      {noticeVisible && (
        <div className="chat-action-notice" data-testid="chat-action-notice">
          <p>
            我把这个决定放在「{noticeTitle}」节点，确认后我会据此{noticeImpact}。
          </p>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              if (noticeTarget) focusQuestion(noticeTarget);
              else select(spaceId);
            }}
          >
            定位到节点
          </button>
        </div>
      )}

      <div className="conversation-history" ref={history}>
        {historyLoading && <p className="turn-loading">正在读取对话…</p>}
        {/* 阶段 12:极轻量的 intake 进度。不是卡片、不占节点空间。 */}
        {primaryIntake && (
          <p className="conversation-intake-progress" data-testid="intake-progress" role="status">
            正在梳理目标 · {intakeAsked}/{intakeLimit}
          </p>
        )}

        {/* 空状态。它本来就锚在顶部(不是垂直居中),所以"下面一大片空白"的成因
            不是位置 —— 是**内容太薄**:一段小字加一句示例,撑不满下面那一大块。
            按这个面板真实能做的事列三条,把洞填上。

            这三条**全是面板里本来就存在的文案**,不是新编的能力:
            第 1 条来自这一段原来的那句话,第 2 条来自 `.replan-row` 的按钮,
            第 3 条来自 `.context-hint`。编一条做不到的事写在这里,比留一片空白更坏。 */}
        {!historyLoading && !messages.length && (
          <div className="conversation-empty">
            <BrandMark size={24} />
            <strong>说说你想推进什么</strong>
            <p>比如「我想在三个月内完成一个 Python 项目」。</p>
            <ul className="conversation-abilities">
              <li><span className="tiny-dot" />先问清楚截止时间、每周能投入多少时间、现在的水平，再动手排计划</li>
              <li><span className="tiny-dot" />按最近的执行情况调整计划</li>
              <li><span className="tiny-dot" />选择画布中的节点，让讨论更聚焦</li>
            </ul>
          </div>
        )}

        {/* 更早的消息没有一起返回。**必须说出来。**
            不说的话,列表的第一条就是一句没头没尾的话(「好的,那我按每周 4 小时
            排」),而用户会以为自己以前的记录丢了 —— 或者更糟,以为 AI 突然开始
            答非所问。 */}
        {messagesTruncated && (
          <p className="conversation-truncated">
            这里只显示了最近 {messages.length} 条。更早的对话还在，只是没有一起取回来。
          </p>
        )}

        {messages.map(m => {
          const remote = m.proposalId ? remoteProposals.find(p => p.id === m.proposalId) : undefined;
          return (
            <article className={`message ${m.role}${m.pending ? ' pending' : ''}${m.failed ? ' failed' : ''}`} key={m.id}>
              <div className="message-byline">
                {m.role === 'assistant'
                  ? <><BrandMark size={20} /><strong>知途</strong><span>与你一起</span></>
                  : <><span className="user-dot">我</span><strong>我</strong></>}
              </div>

              <div className="message-text">{m.text}</div>

              {/* 来源徽标。只在助手的回复上出现,而且历史消息同样显示。 */}
              {m.role === 'assistant' && m.source && (
                <div className={`source-badge${m.degraded ? ' degraded' : ''}`}>
                  {sourceLabel(m.source)}
                  {m.degraded && m.degradedReason ? ` · ${degradedHint(m.degradedReason)}` : ''}
                </div>
              )}
              {/* 服务端验证过的公开来源。**在来源徽标之下** —— 先说"这句话是谁
                  生成的",再说"它依据了什么";顺序反了的话,用户会拿一段模型的
                  推断去核对来源。 */}
              {m.role === 'assistant' && m.research && <ResearchCitations research={m.research} />}
              {m.failed && <div className="message-note failed">这一条没有发出去。</div>}
              {m.pending && <div className="message-note">已记录，正在等 AI 回复…</div>}

              {/* 阶段 12:关键问题就在这条助手消息下面 —— 判断在上、问题与快捷回复
                  紧随其后。它不是卡片,也不是后端问题实体。 */}
              {m.role === 'assistant' && primaryIntake && m.id === intakeMessageId && (
                <div className="intake-inline" data-testid="intake-inline">
                  {!m.text.includes(primaryIntake.question) && (
                    <p className="intake-question">{primaryIntake.question}</p>
                  )}
                  <IntakeChips
                    replies={primaryIntake.quickReplies}
                    disabled={sending}
                    onSend={text => { void send(text); }}
                  />
                </div>
              )}

              {/* 这里曾经还有一张"本地提案"卡片(`proposals` / `accept` /`previewProposal`)。
                  它和下面这张后端的提案卡片**不是同一个东西**,只是名字像:那个是示例
                  空间里本地编出来的"把某个节点挪一挪",确认了也只改浏览器内存。示例空间
                  删掉之后它没有生产者了,一并删掉 —— 留着两张长得像、坏得不一样的卡片,
                  比少一张更难查。 */}

              {/* 后端提案:AI 想对计划做的变更。**要用户点"确认"才写进计划** ——
                  模型不能替用户调这个接口,这是产品规则不是技术细节。 */}
              {remote && (
                <div className="proposal">
                  <span className="eyebrow">AI 提议的变更</span>
                  <strong>
                    {remote.itemCount > 0
                      ? `${remote.itemCount} 项变更`
                      : '这次没有可执行的变更'}
                  </strong>
                  {remote.items.length > 0 && (
                    <ul className="proposal-items">
                      {/* 摘要由**服务端**生成(它知道每一项到底指向哪个节点),
                          前端只负责显示。在前端把 op 和参数拼成一句话的话,
                          两个地方就得各维护一套"人话",迟早对不上。 */}
                      {remote.items.slice(0, 8).map(item => (
                        <li key={item.ordinal}>
                          {item.summary}
                          {typeof item.payload.description === 'string' && item.payload.description && (
                            <em className="proposal-item-detail">{item.payload.description}</em>
                          )}
                        </li>
                      ))}
                      {remote.items.length > 8 && <li>…还有 {remote.items.length - 8} 项</li>}
                    </ul>
                  )}
                  {remote.status === 'validated' || remote.status === 'pending_confirmation'
                    ? <div>
                        <button disabled={deciding} onClick={() => void rejectRemote(remote.id)}>先不要</button>
                        <button className="primary-button" disabled={deciding} onClick={() => void confirmRemote(remote.id)}>
                          {deciding ? '处理中…' : '确认，写入计划'}
                        </button>
                      </div>
                    : <span className="proposal-status">{PROPOSAL_STATUS_LABEL[remote.status] ?? remote.status}</span>}
                </div>
              )}
            </article>
          );
        })}

        {/*
         * 当前动作处理完之后,历史里留一句短摘要。
         *
         * 它对应的是**同一个** `currentInteraction`:active 时由输入框上方的固定卡
         * (或专注弹层)完整承载,answered / confirmed 之后固定卡整个不渲染(见
         * `CurrentInteractionCard`),这里补一句可回看的结论 —— 既不把旧问题留在
         * 页面上遮挡,也不让用户以为它从没存在过。
         */}
        {currentInteraction && currentInteraction.status !== 'active' && (
          <p className="conversation-interaction-note" data-testid="current-interaction-resolved" role="status">
            {currentInteraction.status === 'confirmed'
              ? '已确认'
              : currentInteraction.status === 'answered'
                ? '已回答'
                : '已处理'}
            ：{currentInteraction.title}
          </p>
        )}

        {/* 这一轮是在一份**已经过去的输入**上回答的。
            它必须单独说一句,而且必须在"没有提案"那一片空白之前说:
            `INPUT_CHANGED` 拦下来的那一轮通常什么都不提,于是界面上"这次没有提案"
            与"模型什么都没想出来"长得一模一样 —— 用户会以为自己白问了,而真正该做
            的是改完之后让 AI 重看一遍。

            **它只是一行结论,不再是大块长文。** 想看真实执行边界发生了什么,走
            「运行记录」;诊断入口关闭时退回一句可执行的建议。 */}
        {inputChanged && (
          <div className="turn-error turn-error-compact" role="status">
            <AlertCircle size={14} />
            <span>本轮未应用：信息已更新</span>
            {traceAvailability.status === 'enabled' ? (
              <button type="button" className="turn-error-toggle" onClick={openTrace}>查看运行记录</button>
            ) : (
              <span>，请到节点的「AI 分析」里重新分析</span>
            )}
          </div>
        )}

        {/* 模型提了变更、但校验没让过。**必须说出来,而且必须收得住。**

            旧版把全部原因一次性铺在对话流里,而校验文案可能很长 —— 用户第一眼
            读到的是"系统出错了",而不是"这次建议没生效、计划没变"。新版默认
            只留一行结论,原因点开才看,而且能关掉。关闭只是关掉这条前端提示,
            不改提案状态、也不伪造成功。见 `ProposalErrorNotice`。 */}
        {proposalErrors.length > 0 && (
          <ProposalErrorNotice
            key={proposalErrors.map(error => `${error.code}:${error.message}`).join('|')}
            errors={proposalErrors}
          />
        )}

        {/* 还没有任何一条消息指向它的提案。
            **这份渲染不是锦上添花,是"按执行情况调整"能被用起来的前提。**
            上面那份渲染是靠 `m.proposalId` 找到提案的,只按消息找的话,一份
            消息已经落在窗口外的提案会静静地躺在库里 —— 用户看不到,也点不了
            确认,甚至连"系统提过调整"这件事都不知道。 */}
        {orphanProposals.map(proposal => (
          <article className="message assistant" key={proposal.id}>
            <div className="message-byline"><BrandMark size={20} /><strong>知途</strong><span>按你的执行情况</span></div>
            <div className="message-text">
              我看了最近的执行情况，提出下面这些调整。你看过之后再决定要不要写进计划。
            </div>
            <div className="proposal">
              <span className="eyebrow">AI 提议的变更</span>
              <strong>{proposal.itemCount > 0 ? `${proposal.itemCount} 项变更` : '这次没有可执行的变更'}</strong>
              {proposal.items.length > 0 && (
                <ul className="proposal-items">
                  {proposal.items.slice(0, 8).map(item => (
                    <li key={item.ordinal}>
                      {item.summary}
                      {typeof item.payload.description === 'string' && item.payload.description && (
                        <em className="proposal-item-detail">{item.payload.description}</em>
                      )}
                    </li>
                  ))}
                  {proposal.items.length > 8 && <li>…还有 {proposal.items.length - 8} 项</li>}
                </ul>
              )}
              <div>
                <button disabled={deciding} onClick={() => void rejectRemote(proposal.id)}>先不要</button>
                <button className="primary-button" disabled={deciding} onClick={() => void confirmRemote(proposal.id)}>
                  {deciding ? '处理中…' : '确认，写入计划'}
                </button>
              </div>
            </div>
          </article>
        ))}

        {/* 失败是可见的状态,不是一句被吞掉的异常。 */}
        {sendError && (
          <div className="turn-error" role="alert">
            <AlertCircle size={14} />
            <span>{sendError}{!retryable && ' 这一条重试也不会成功。'}</span>
            <button type="button" disabled={!retryable || sending} onClick={() => void retry()}>
              <RotateCcw size={12} />重试
            </button>
          </div>
        )}

        {/* 阶段 11:模型请求进行中,在最后一条消息附近给一句克制的实时状态。
            失败与重试由下面的 `sendError` 行负责,不在这里重复。 */}
        {showLive && (
          <p className="conversation-live" data-testid="conversation-live" role="status" aria-live="polite">
            <span className="tiny-dot" aria-hidden="true" />
            {intakeActive ? '正在形成整体判断…' : agentStatus.text}
          </p>
        )}

        {/*
         * 规划智能体重构 V1 Stage 1:战略判断 / 候选方向 / 目标确认 / 关键问题。
         *
         * **放在滚动区里**,不是 composer 上方 —— 否则内容一多会把输入框顶出屏幕,
         * 而且这一层在浮动面板里是 `pointer-events:none` 的兄弟区域,会吞掉点击。
         */}
        <div className="v1-stage1">
          {/*
           * 对话区**不再承载战略仪表盘**。
           *
           * “我的理解 / 当前战略判断 / 已采用的起点 / 战略路径草案”这些常驻大卡片
           * 已删除;判断与解释改由 AI 的聊天消息承担,结构化问答只在画布节点里。
           * 这里只保留失败/等待这类**短状态**,以及执行阶段的三个生成入口。
           */}
          {v1StatusFailed && (
            <div className="v1-status" role="status">
              <span>{reasoning?.v1Error ?? '这次战略判断没有完成。'}</span>
              <button type="button" disabled={sending} onClick={() => void retry()}>
                重试
              </button>
            </div>
          )}

          {reasoning?.v1Status === 'awaiting_user_confirmation' &&
            reasoning?.v1Stage === 'coarse_timeline_review' && (
              <div
                className="v1-status v1-awaiting"
                data-testid="v1-awaiting-confirmation"
                role="status"
              >
                <span>时间架构已准备好，请在「排出来」节点确认。</span>
              </div>
            )}
        </div>

        <div ref={bottom} />
      </div>

      <div className="composer-area">
        {/* 问题入口已经移到标题栏下方那条紧凑状态条(见 `QuestionStatusBar`)。
            这里不再重复渲染问题正文 —— 完整回答只在画布的 Question Node 里完成。 */}
        {/* 「按执行情况调整」的入口。
            放在对话里而不是排期页,是因为它的产出是一份**要用户确认的提案**,
            而确认的界面就在这里 —— 换个地方发起、再让用户回来确认,中间那一步
            用户是会丢的。

            **只有真有东西可调时才出现。** 没有已确认战略,这条链路没有判断依据;
            没有执行计划,也就没有可调整的对象(见 `canReplan`)。 */}
        {canReplan && (
          <div className="replan-row">
            <button type="button" className="text-button" disabled={replanState.busy || sending} onClick={() => void replan()}>
              <RefreshCw size={12} />{replanState.busy ? '正在看最近的执行情况…' : '按最近的执行情况调整计划'}
            </button>
            {/* 降级时说"这次没能给出方案",不说"不需要调整" —— 前者要用户重试,
                后者要用户放心,这两句话差别很大,不能合成一句空白。 */}
            {replanState.message && (
              <p className={replanState.degraded ? 'replan-note degraded' : 'replan-note'} role="status">
                {replanState.message}
              </p>
            )}
          </div>
        )}

        {/* 「选择画布中的节点,让讨论更聚焦」这条说明只在**确实有事可做**时出现:
            已经选了一个节点(`.context-chip`),或者有一个当前待回答的问题。既没有
            问题也没有选中项时,它是一句指向不存在动作的常驻空话 —— 不渲染。 */}
        {selected
          ? <div className="context-chip"><span className="tiny-dot" />正在讨论：{selected.title}<button aria-label="清除上下文" onClick={() => select(null)}><X size={12} /></button></div>
          : primaryQuestion
            ? <div className="context-hint"><span className="tiny-dot" />选择画布中的节点，让讨论更聚焦</div>
            : null}

        {showContexts && (
          <div className="context-options">
            {Object.values(growth.nodes).filter(node => node.type === 'capability').map(node => (
              <button key={node.id} onClick={() => { select(node.id); setShowContexts(false); }}>{node.title}</button>
            ))}
          </div>
        )}

        {v1Initial && (
          <div className="v1-initial-guide" data-testid="v1-initial-guide">
            <span className="eyebrow">先想清楚，再排出来</span>
            <p>{V1_READY_PROMPT}</p>
            <p className="v1-initial-hint">回复「开始 / 准备好了」就行；想先补充背景也可以直接说。</p>
          </div>
        )}

        <form className={v1Initial ? 'composer is-v1-initial' : 'composer'} onSubmit={e => { e.preventDefault(); submit(); }}>
          <textarea
            ref={composer}
            rows={1}
            aria-label="给 AI 的消息"
            placeholder={v1Initial ? '比如：我想学 Python，因为想自己做数据分析，但担心坚持不下来……' : selected ? `关于「${selected.title}」，告诉 AI 你的想法……` : '我想在……之内完成……'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }}
          />
          <div className="composer-actions">
            <button type="button" className="icon-button" aria-label="添加讨论对象" onClick={() => setShowContexts(!showContexts)}><Plus size={18} /></button>
            <span><CornerDownLeft size={11} />发送 · Shift + Enter 换行</span>
            <button type="submit" aria-label="发送消息" className="send-button" disabled={!input.trim() || sending}><ArrowUp size={18} /></button>
          </div>
        </form>
        {/* 这里不再写死"DeepSeek" —— 每一轮到底是谁生成的,由消息上方的徽标说。 */}
        <p className="composer-footnote">一起思考，由你决定。<span>回复会写明来源</span></p>
      </div>
    </aside>
  );
}
