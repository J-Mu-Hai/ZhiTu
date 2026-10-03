'use client';
import { useEffect, useState } from 'react';
import { AlertCircle, Copy, RotateCcw, X } from 'lucide-react';
import { useDemo } from '@/features/growth/provider';
import type { AgentTraceTurnView } from '@/lib/backend';

/**
 * 本地 Agent 运行记录(阶段 9)。
 *
 * ## 它是什么、不是什么
 *
 * 它是一份**只读诊断投影**:服务端真实执行到哪一步、用了多久、工具结果如何、终态是
 * 什么。它**不是思维链** —— 契约里没有 prompt、模型原始输出、用户原文、密钥或
 * Cookie(见 `backend/contracts/trace.py`),这里也不可能把它们显示出来。
 *
 * ## 轮询只在有 turn 跑着时开
 *
 * 关闭轮询的判据是服务端返回的 `status === 'running'`,不是前端动画。终态一到就停,
 * 不会用"还在工作"的假状态骗用户。
 *
 * ## 复制诊断摘要
 *
 * 它是 Pi / 维护者排障的交接入口:**只含短 ID、触发来源、状态序列、耗时、错误码、
 * 工具摘要与当前开关**,不含任何敏感内容。
 */

const STEP_LABEL: Record<string, string> = {
  queued: '排队',
  resolving_context: '准备上下文',
  waiting_model: '等待模型',
  running_tool: '执行工具',
  validating_output: '校验输出',
  persisting: '写入',
  completed: '已完成',
  failed: '失败',
  timed_out: '超时',
  cancelled: '已取消',
  unavailable: '无轨迹',
};

const STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  timed_out: '超时',
  cancelled: '已取消',
  unavailable: '无轨迹',
};

/** 耗时只在终态时显示,而且直接用服务端给的毫秒。 */
function durationLabel(turn: AgentTraceTurnView): string {
  if (turn.status === 'running') {
    return turn.waitingSeconds != null ? `运行中 · 已等待 ${turn.waitingSeconds} 秒` : '运行中';
  }
  return turn.durationMs != null ? `${(turn.durationMs / 1000).toFixed(1)} 秒` : '—';
}

/**
 * 复制用的脱敏摘要。
 *
 * **字段是白名单。** 加字段之前先问一句"它会不会带出模型/用户原文";会的话就不加。
 */
function diagnosticSummary(workspaceShortId: string, turn: AgentTraceTurnView): string {
  return JSON.stringify(
    {
      workspace: workspaceShortId,
      turn: turn.shortId,
      trigger: turn.trigger,
      status: turn.status,
      currentStep: turn.currentStep,
      steps: turn.steps,
      attempt: turn.attempt,
      durationMs: turn.durationMs,
      terminalCode: turn.terminalCode,
      safeSummary: turn.safeSummary,
      tools: turn.tools.map(tool => ({
        tool: tool.toolName,
        status: tool.status,
        durationMs: tool.durationMs,
        summary: tool.summary,
      })),
    },
    null,
    2,
  );
}

function TurnCard({ turn, workspaceShortId }: { turn: AgentTraceTurnView; workspaceShortId: string }) {
  const { retryTraceTurn, sending } = useDemo();
  const [copied, setCopied] = useState(false);
  const failed = turn.status === 'failed' || turn.status === 'timed_out';

  async function copy() {
    const text = diagnosticSummary(workspaceShortId, turn);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 剪贴板权限被拒时,退回到一个可手动复制的不透明文本框 —— 静默失败更糟。
      window.prompt('复制这段诊断摘要', text);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  }

  return (
    <article
      className={`trace-turn trace-turn-${turn.status}`}
      data-testid="trace-turn"
      data-status={turn.status}
      data-trigger={turn.trigger}
    >
      <div className="trace-turn-head">
        <span className="trace-trigger">{turn.triggerLabel}</span>
        <span className={`trace-status trace-status-${turn.status}`}>{STATUS_LABEL[turn.status] ?? turn.status}</span>
        <span className="trace-duration">{durationLabel(turn)}</span>
      </div>
      <p className="trace-step">
        {turn.stepLabel}
        {turn.attempt > 1 && <span className="trace-attempt"> · 第 {turn.attempt} 次尝试</span>}
        {turn.waitingTooLong && <span className="trace-waiting"> · 仍在等待模型响应</span>}
      </p>
      {turn.safeSummary && <p className="trace-summary">{turn.safeSummary}</p>}
      {turn.tools.length > 0 && (
        <ul className="trace-tools">
          {turn.tools.map((tool, index) => (
            <li key={`${tool.toolName}-${index}`} data-status={tool.status}>
              <span>{tool.summary}</span>
              {tool.durationMs != null && <time>{tool.durationMs} ms</time>}
            </li>
          ))}
        </ul>
      )}
      <div className="trace-actions">
        <button type="button" className="trace-action" onClick={() => void copy()}>
          <Copy size={12} />{copied ? '已复制' : '复制诊断摘要'}
        </button>
        {turn.retryable && (
          <button
            type="button"
            className="trace-action trace-retry"
            disabled={sending}
            onClick={() => void retryTraceTurn(turn)}
          >
            <RotateCcw size={12} />重试
          </button>
        )}
      </div>
      {turn.steps.length > 0 && (
        <details className="trace-steps">
          <summary>状态序列</summary>
          <ol>
            {turn.steps.map((step, index) => (
              <li key={`${step}-${index}`}>{STEP_LABEL[step] ?? step}</li>
            ))}
          </ol>
        </details>
      )}
      {failed && turn.terminalCode && <span className="trace-code">{turn.terminalCode}</span>}
    </article>
  );
}

export function AgentTraceInspector() {
  const { trace, traceOpen, traceDisabled, traceError, traceLoading, closeTrace, refreshTrace } = useDemo();

  useEffect(() => {
    if (!traceOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeTrace();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [traceOpen, closeTrace]);

  if (!traceOpen) return null;

  return (
    <div className="trace-overlay" data-testid="trace-overlay" onClick={closeTrace}>
      <aside
        className="trace-inspector"
        role="dialog"
        aria-modal="true"
        aria-label="运行记录"
        data-testid="trace-inspector"
        onClick={event => event.stopPropagation()}
      >
        <header className="trace-header">
          <div>
            <h2>运行记录</h2>
            <p>只显示系统可审阅的状态、耗时与工具摘要，不含模型原始内容。</p>
          </div>
          <button type="button" className="icon-button" aria-label="关闭运行记录" onClick={closeTrace}>
            <X size={16} />
          </button>
        </header>

        <div className="trace-body">
          {traceDisabled && (
            <p className="trace-empty" role="status">本地诊断入口没有开启。</p>
          )}
          {!traceDisabled && traceError && (
            <div className="turn-error" role="alert">
              <AlertCircle size={14} />
              <span>{traceError}</span>
              <button type="button" onClick={() => void refreshTrace()}>重试</button>
            </div>
          )}
          {!traceDisabled && !traceError && traceLoading && !trace && (
            <p className="trace-empty" role="status">正在读取运行记录…</p>
          )}
          {!traceDisabled && trace && trace.turns.length === 0 && (
            <p className="trace-empty" role="status">还没有运行记录。发一条消息后再看。</p>
          )}
          {!traceDisabled && trace?.turns.map(turn => (
            <TurnCard key={turn.id} turn={turn} workspaceShortId={trace.workspaceShortId} />
          ))}
        </div>
      </aside>
    </div>
  );
}
