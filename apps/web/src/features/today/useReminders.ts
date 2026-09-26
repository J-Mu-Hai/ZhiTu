'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import * as backend from '@/lib/backend';
import { ApiError } from '@/lib/api';

/**
 * 站内提醒。**「站内」这两个字是承诺,不是修饰。**
 *
 * 它只在用户打开界面时出现 —— 关掉浏览器就收不到,后端也没有任何定时任务。
 * 所以界面上的文案不能出现"已提醒""稍后会通知你"这类听起来像推送的话
 * (「稍后」是"下次打开时再出现",这个说法是真的)。
 *
 * 提醒本身**不是存下来的数据**,是每次请求按事实重算的(空间还空着、计划刚更新、
 * 几天没记录……),库里只存用户"关掉了哪条、让哪条过一会儿再说"。所以刷新页面
 * 不会让关掉的提醒复活 —— 那是服务端记住的,不是本地状态。
 */
export function useReminders(enabled: boolean) {
  const [data, setData] = useState<backend.RemindersResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** 正在处置的那一条。按 `key` 而不是布尔 —— 一次只处置一条,其余不该跟着变灰。 */
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const inflight = useRef(new Set<string>());

  const refresh = useCallback(async () => {
    try {
      setData(await backend.fetchReminders());
      setError(null);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : '读取提醒失败。');
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      setData(null);
      setError(null);
      return;
    }
    void refresh();
  }, [enabled, refresh]);

  const act = useCallback(
    async (key: string, action: 'dismiss' | 'snooze') => {
      if (inflight.current.has(key)) return;
      inflight.current.add(key);
      setBusyKey(key);
      try {
        if (action === 'dismiss') await backend.dismissReminder(key);
        else await backend.snoozeReminder(key);
        setError(null);
        // 重新拉而不是本地把这条从数组里删掉:提醒是**推导**出来的,本地删一条
        // 只是把屏幕改好看了一点 —— 下次刷新它还在,用户会以为"关掉"没生效。
        await refresh();
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : '操作失败，请重试。');
      } finally {
        inflight.current.delete(key);
        setBusyKey(null);
      }
    },
    [refresh],
  );

  return {
    reminders: data?.reminders ?? [],
    quietHours: data?.quietHours ?? null,
    suppressedCount: data?.suppressedCount ?? 0,
    note: data?.note ?? null,
    error,
    busyKey,
    dismiss: useCallback((key: string) => act(key, 'dismiss'), [act]),
    snooze: useCallback((key: string) => act(key, 'snooze'), [act]),
  };
}
