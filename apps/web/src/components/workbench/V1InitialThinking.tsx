'use client';
import { useState } from 'react';
import { ArrowUp } from 'lucide-react';

/**
 * 规划智能体重构 V1(P1):阶段一「初步思考」的**中央大输入区**。
 *
 * ## 它和 V0.1 的 DiscoveryPrompt 有什么不同
 *
 * V0.1 那一版一次抛出 2–4 个核心问题,用户一次答完。V1 这一版**只给引导,不给问题
 * 清单**:让用户补充“为什么是现在、真正希望得到什么、担心什么”。提交后才由 AI
 * 给出一段整体判断与**一个**全局关键问题。
 *
 * ## 它会消失
 *
 * 用户提交后 `reasoning.v1Stage` 不再是 `initial_thinking`,这个组件就不再渲染 ——
 * 同一份状态换成三组折叠画布。转场靠状态,不靠动画堆叠。
 */
export function V1InitialThinking({
  busy,
  onSend,
}: {
  busy: boolean;
  onSend: (text: string) => void;
}) {
  const [text, setText] = useState('');
  const ready = text.trim().length > 0 && !busy;
  return (
    <div className="v1-thinking" data-testid="v1-initial-thinking">
      <div className="v1-thinking-inner">
        <span className="eyebrow">先想清楚，再排出来</span>
        <h2>在动手排计划之前，先把这件事想清楚</h2>
        <p className="v1-thinking-lead">
          不用写成正式目标，把下面三点里你最清楚的先说给我：
        </p>
        <ul className="v1-thinking-prompts">
          <li><strong>为什么是现在</strong>——是什么让你此刻想开始？</li>
          <li><strong>你真正希望得到什么</strong>——最后能拿出什么，才算解决了问题？</li>
          <li><strong>你担心什么</strong>——最怕哪一步做不下去？</li>
        </ul>
        <form
          className="v1-thinking-form"
          onSubmit={event => {
            event.preventDefault();
            if (!ready) return;
            onSend(text.trim());
            setText('');
          }}
        >
          <textarea
            aria-label="初步思考"
            placeholder="比如：我想学 Python，因为想自己做数据分析，但担心坚持不下来……"
            value={text}
            rows={4}
            onChange={event => setText(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                if (ready) { onSend(text.trim()); setText(''); }
              }
            }}
          />
          <button type="submit" aria-label="开始想清楚" disabled={!ready}>
            <ArrowUp size={18} />
            {busy ? '正在判断…' : '开始想清楚'}
          </button>
        </form>
        <p className="v1-thinking-note">
          这一步不会生成任务、课程或时间线；只会给出一个整体判断和一个最关键的问题。
        </p>
      </div>
    </div>
  );
}
