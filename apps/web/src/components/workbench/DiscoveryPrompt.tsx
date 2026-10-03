'use client';
import { useState } from 'react';
import { ArrowUp } from 'lucide-react';

/**
 * 规划智能体 V0.1:阶段一(DISCOVERY)的**中央大输入框**。
 *
 * ## 它为什么在这里,而不是画布上
 *
 * 新目标刚说出口时,用户要做的是“把目标讲清楚”,而不是看画布。所以这一屏
 * 画布保持干净:没有时间线、没有任务、没有规划节点,只有 AI 已经问出的
 * 2–4 个核心问题和一个可以一次答完的输入框。
 *
 * ## 它会消失
 *
 * 用户回答、阶段一完成之后,`workflowStage` 不再是 `discovery`,这个组件就不再
 * 渲染 —— 输入框“缩回”到右侧固定的 ConversationPanel,而 discovery 节点出现在
 * 画布上。转场不靠动画堆叠,靠的是**同一份状态换了呈现**。
 */
export function DiscoveryPrompt({
  questions,
  busy,
  onSend,
}: {
  questions: string[];
  busy: boolean;
  onSend: (text: string) => void;
}) {
  const [text, setText] = useState('');
  const ready = text.trim().length > 0 && !busy;
  return (
    <div className="discovery-prompt" data-testid="discovery-prompt">
      <div className="discovery-prompt-inner">
        <span className="eyebrow">先对齐目标</span>
        <h2>先把这几件事说清楚，我再排出整条时间线</h2>
        {questions.length > 0 && (
          <ul className="discovery-prompt-questions">
            {questions.map(question => (
              <li key={question}>{question}</li>
            ))}
          </ul>
        )}
        <form
          className="discovery-prompt-form"
          onSubmit={event => {
            event.preventDefault();
            if (!ready) return;
            onSend(text.trim());
            setText('');
          }}
        >
          <textarea
            aria-label="回答关键问题"
            placeholder="一次说清就行：想拿到什么、大概多久、现在的基础……"
            value={text}
            rows={3}
            onChange={event => setText(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (ready) { onSend(text.trim()); setText(''); }
              }
            }}
          />
          <button type="submit" aria-label="开始规划" disabled={!ready}>
            <ArrowUp size={18} />
            {busy ? '正在生成规划节点…' : '开始规划'}
          </button>
        </form>
      </div>
    </div>
  );
}
