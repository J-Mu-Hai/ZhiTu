'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import * as backend from '@/lib/backend';
import { ApiError } from '@/lib/api';

/**
 * 首页「本周计划 / 本日计划」两个页签的聚合数据。
 *
 * 只读 `GET /api/today/plans` —— 一次拿到全部活动空间的本周计划与今天安排,不自
 * 己逐空间拉 `/plan`。写入(勾选完成、添加任务)由调用方走正式接口,成功后调这里
 * 的 `refresh()` 重取。
 */
export function useTodayPlans() {
  const [data, setData] = useState<backend.TodayPlansResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  /** 只允许最后一次读取落到界面 —— 连续刷新时先发的可能后回。 */
  const version = useRef(0);

  const refresh = useCallback(async () => {
    const current = ++version.current;
    setLoading(true);
    try {
      const next = await backend.fetchTodayPlans();
      if (current === version.current) {
        setData(next);
        setError(null);
      }
    } catch (cause) {
      if (current === version.current) {
        setError(cause instanceof ApiError ? cause.message : '读取计划失败,请重试。');
      }
    } finally {
      if (current === version.current) setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  return { data, error, loading, refresh, setError };
}
