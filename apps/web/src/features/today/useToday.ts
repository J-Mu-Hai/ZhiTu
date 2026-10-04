'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import * as backend from '@/lib/backend';
import { ApiError } from '@/lib/api';
import { useDemo } from '@/features/growth/provider';

/**
 * 「今天」的真实数据,以及往上报一次执行结果。
 *
 * ## 为什么这个页面以前在"假装保存"
 *
 * 今天页原来只读本地 `growth` 状态,勾一下完成就是一次 `apply({type:'UPDATE_STATUS'})`
 * —— **一个只改内存的 reducer**。对示例空间成立(它整份都在浏览器里),对真实空间
 * 就是假的:界面显示"完成了",库里什么都没有,刷新就回来。而"根据执行情况持续调整"
 * 这条闭环的起点,恰恰是"用户报告了实际发生了什么" —— 那一环根本没接上。
 *
 * 所以现在只有这一条路:`GET /api/today` 读跨空间的今天,`POST /sessions/{id}/executions`
 * 把结果写进去。那份只改内存的本地实现随示例空间一起没有了 —— 留着它,它就会是
 * 下一个"界面显示完成了、库里什么都没有"的地方。
 *
 * ## 未记录 ≠ 未完成
 *
 * `TodayItemView.result === null` 的含义是**没有任何记录**,不是"没做";`recorded`
 * 字段让这件事更难被漏判。所以界面不能把没记录的显示成"未完成",只能**问**
 * (`checkInQuestions` 就是服务端拼好的那句问话)。
 */

/** 把一次失败翻译成给人看的一句话。`ApiError` 已经带着后端给的 code 与中文 message。 */
function describe(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : '出了点问题，请重试。';
}

type RecordExtra = Partial<Omit<backend.RecordExecutionPayload, 'result' | 'idempotencyKey'>>;

export function useToday(enabled: boolean) {
  // 时间线里手工排的工作块不推计划版本,所以这里单独听一个计数 —— 它一变就重拉
  // `/api/today`。不引入 WebSocket:同页与切页都以"重新拉取"为准。
  const { todayRevision } = useDemo();
  const [data, setData] = useState<backend.TodayResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  /** 正在写入的那一场 —— 界面据此把那一行的按钮换成"记录中"。 */
  const [savingSession, setSavingSession] = useState<string | null>(null);
  /** 只允许最后一次读取落到界面。首次加载可能比记录完成后的刷新更晚返回。 */
  const refreshVersion = useRef(0);

  /**
   * 幂等键:每个「场次 + 结果」一个,**成功之后作废**。
   *
   * 失败时保留 —— 用户点"重试"必须复用同一个键,否则后端会把同一次反馈当成两次,
   * 库里多出一条。
   *
   * 成功之后清掉,是因为同一场再记一次是**合法且必要**的:分两次做完
   * (先 20 分钟、再 30 分钟)本来就该是两条记录,而场次上的 `actualMinutes` 是 50。
   * 只留最后一次的话,一去不返的是过程。
   */
  const keys = useRef(new Map<string, string>());
  /**
   * 正在飞的请求。挡"双击"靠的是这个 ref 而不是 state —— state 要等一次渲染,
   * 而两次点击可以落在同一帧里。
   */
  const inflight = useRef(new Set<string>());

  const refresh = useCallback(async () => {
    const version = ++refreshVersion.current;
    setLoading(true);
    try {
      const next = await backend.fetchToday();
      if (version === refreshVersion.current) {
        setData(next);
        setError(null);
      }
    } catch (cause) {
      if (version === refreshVersion.current) setError(describe(cause));
    } finally {
      if (version === refreshVersion.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      refreshVersion.current += 1;
      // 没有空间可看时把上一次的数据清掉 —— 否则换个账户登录,「今天」还挂着
      // 上一个账户的安排。
      setData(null);
      setError(null);
      return;
    }
    void refresh();
  }, [enabled, refresh, todayRevision]);

  const record = useCallback(
    async (sessionId: string, result: backend.ExecutionResult, extra: RecordExtra = {}) => {
      const slot = `${sessionId}:${result}`;
      if (inflight.current.has(slot)) return false;

      inflight.current.add(slot);
      setSavingSession(sessionId);
      const key = keys.current.get(slot) ?? crypto.randomUUID();
      keys.current.set(slot, key);

      try {
        const response = await backend.recordExecution(sessionId, {
          result,
          idempotencyKey: key,
          ...extra,
        });
        if (!response.saved) {
          // 后端写不进库时给的是 503 + saved:false,走的是下面的 catch。真出现
          // "200 但 saved=false" 就如实说出来,不能当成记完了 —— 那条记录会永久消失,
          // 而用户以为它在。
          setError('这条记录没有落库，请重试。');
          return false;
        }
        keys.current.delete(slot);
        setError(null);
        await refresh();
        return true;
      } catch (cause) {
        setError(describe(cause));
        return false;
      } finally {
        inflight.current.delete(slot);
        setSavingSession(null);
      }
    },
    [refresh],
  );

  return {
    data,
    error,
    loading,
    savingSession,
    refresh,
    record,
    clearError: useCallback(() => setError(null), []),
  };
}
