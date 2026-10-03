'use client';
import { AlertCircle, CheckCircle2, RotateCcw, X } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';

/**
 * Agent 运行的**持续可见状态条**(阶段 9.2)。
 *
 * ## 它解决什么
 *
 * 此前只有普通对话发送会显示“正在思考”(`sending` 驱动),自动 `space_entered`、
 * `regenerate_roadmap`、`question_answered`、`confirm`、`refine`、Trace 重试这些
 * 地图轮在界面上**没有任何运行中反馈** —— 用户只能盯着空白对话区猜是不是卡住了。
 *
 * 这条状态条由统一的 `agentStatus` 驱动,覆盖全部 Agent 调用。它的内容**只来自
 * 服务端轨迹**:请求刚发出、轨迹还没落库时只能显示“正在启动”;轨迹出现后显示真实的
 * `currentStep` 与服务端算出的等待秒数;超过 25 秒仍是 `waiting_model` 时给出
 * “仍在等待模型响应”和“打开运行记录”。**不自己伪造进度,也不无限转圈。**
 *
 * ## 为什么图标不动画
 *
 * 状态本身已经说明了在做什么。再加一个转圈,只会和 `floating-title` 里那条
 * “正在思考”的线抢注意力,而且“还在转”不等于“还在推进”。这里用静态图标。
 */
export function AgentActivityBar() {
  const { agentStatus, openTrace, dismissAgentStatus, retryAgentFailure } = useDemo();
  if (!agentStatus) return null;
  const { tone, text, closable, retryable } = agentStatus;
  const terminal = tone === 'completed' || tone === 'failed';
  const showTraceLink = terminal || tone === 'waiting_long';

  return (
    <div
      className={`agent-activity agent-activity-${tone}`}
      role="status"
      aria-live="polite"
      data-testid="agent-activity"
      data-tone={tone}
    >
      <span className="agent-activity-icon" aria-hidden="true">
        {tone === 'completed' ? <CheckCircle2 size={13} /> : tone === 'failed' ? <AlertCircle size={13} /> : <span className="agent-activity-dot" />}
      </span>
      <span className="agent-activity-text">{text}</span>
      {showTraceLink && (
        <button type="button" className="agent-activity-link" onClick={openTrace}>
          打开运行记录
        </button>
      )}
      {tone === 'failed' && retryable && (
        <button type="button" className="agent-activity-retry" onClick={() => void retryAgentFailure()}>
          <RotateCcw size={11} />重试
        </button>
      )}
      {closable && (
        <button type="button" className="agent-activity-close" aria-label="关闭状态" onClick={dismissAgentStatus}>
          <X size={11} />
        </button>
      )}
    </div>
  );
}
