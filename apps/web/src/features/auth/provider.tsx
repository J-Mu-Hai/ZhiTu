'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ApiError, clearToken, getToken, setToken, setUnauthorizedHandler } from '@/lib/api';
import * as backend from '@/lib/backend';
import { toAccountProfile, type AccountProfile, type EditableProfile, type RegisterAccountInput } from './types';

/**
 * 账户与会话。**全部走真实后端。**
 *
 * ## 上一版是什么,以及为什么换掉
 *
 * 上一版把账户数组(含口令摘要)和"当前登录的是谁"都放在 localStorage 里,
 * `login()` 就是在浏览器里比对一个 SHA-256。界面底部那句
 * "本地演示不会上传账户和计划"是诚实的 —— 但它的代价是:换个浏览器登录同一个邮箱,
 * 看到的是一个空的新账户;而这个"账户"其实不是账户,只是这台机器上的一条记录。
 *
 * 现在:口令只在请求体里出现一次,令牌存在 localStorage,每次请求带
 * `Authorization: Bearer`。刷新页面时用 `GET /api/users/me` 恢复会话 —— 这一步同时
 * 也在验证那张令牌仍然有效(可能已经在别处被登出、或因为改密码而整批失效)。
 *
 * ## 令牌过期怎么办:只有 401 才清令牌
 *
 * `apiFetch` 遇到 401 会调用这里注册的回调,把用户状态清掉。这样"令牌失效了"
 * 不会表现为一堆莫名其妙的空页面,而是干净地回到登录页。
 *
 * 但**恢复失败不等于令牌失效**,这个区别是这一版补上的,代价很具体:
 * 原来 `catch` 不分青红皂白 `clearToken()`,于是后端重启一下、网线抖一下、
 * 或者一个请求被中断,浏览器里那张令牌就被**删掉**了。用户看到的是"莫名其妙退出了
 * 登录",而且**恢复后再刷新也回不来** —— 令牌已经不在本地了,他只能重新输密码。
 * 部署时重启服务(systemd restart)会让正好在刷新页面的每个用户都撞上这一下。
 *
 * 所以现在按错误类型分两种:
 *
 * - `401` —— 服务端明确拒绝了这张令牌(过期、被撤销、改过密码)。清掉,回登录页。
 * - 其它(连不上、超时、5xx、请求被中断)—— 令牌**留着**,把问题如实说出来,
 *   让用户重试。后端回来了,这一趟就过去了,不需要重新登录。
 *
 * ## 并发恢复:只认最后发起的那一趟
 *
 * 开发模式下 React 会把 effect 跑两遍,于是同一个页面会发两次 `GET /api/users/me`。
 * 两趟的结果都可能回来,而它们可能一好一坏 —— 谁后到谁说了算的话,界面就会在
 * "已登录"和"未登录"之间随机跳动。用一个递增的代号只认最后一趟:先发起的那一趟
 * 无论成功失败都不再写状态。
 */
function useAuthState() {
  const [ready, setReady] = useState(false);
  const [user, setUser] = useState<AccountProfile | null>(null);
  /**
   * "有令牌,但这一次没能拿回档案"的原因。非空时界面显示的是**重试**,
   * 不是登录页 —— 见 AppShell 的那段判断,以及本文件顶部那段注释。
   */
  const [restoreProblem, setRestoreProblem] = useState<string | null>(null);
  // 401 回调里要读最新的 user,但它注册在 useEffect 里只跑一次 —— 用 ref 打破这个闭包。
  const userRef = useRef<AccountProfile | null>(null);
  userRef.current = user;

  //: 恢复会话的代数。只允许最后发起的那一趟写状态。
  const restoreGeneration = useRef(0);

  const restore = useCallback(async () => {
    const generation = ++restoreGeneration.current;
    const isLatest = () => restoreGeneration.current === generation;

    setRestoreProblem(null);
    setReady(false);

    if (!getToken()) {
      // 这台浏览器上没有令牌 —— 那就该去登录页,不是"失败"。
      setUser(null);
      setReady(true);
      return;
    }

    try {
      const profile = await backend.fetchMe();
      if (isLatest()) setUser(toAccountProfile(profile));
    } catch (error) {
      if (!isLatest()) return;
      if (error instanceof ApiError && error.status === 401) {
        clearToken();
        setUser(null);
      } else {
        // 令牌留着。这里**不**把 user 清成 null 之外做任何事 —— 界面上那条重试
        // 提示由 restoreProblem 负责。
        setRestoreProblem(
          error instanceof ApiError ? error.message : '恢复登录状态失败,请重试。',
        );
      }
    } finally {
      if (isLatest()) setReady(true);
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      clearToken();
      setUser(null);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  useEffect(() => {
    void restore();
  }, [restore]);

  const login = useCallback(async (email: string, password: string) => {
    const result = await backend.login(email, password);
    setToken(result.token);
    const profile = toAccountProfile(result.user);
    setUser(profile);
    return profile;
  }, []);

  const register = useCallback(async (input: RegisterAccountInput) => {
    const result = await backend.register({
      email: input.email,
      password: input.password,
      displayName: input.name,
      timezone: backend.browserTimezone(),
    });
    setToken(result.token);

    // 学校/专业/年级/目标这些字段注册接口不收 —— 它们在 PATCH /api/users/me 上。
    // 分两步而不是"注册时一次传完",是因为注册接口的契约只有四个字段;
    // 多传的就是 422(请求模型是 extra="forbid")。
    //
    // 第二步失败不让注册整体失败:账户已经建好了,令牌也拿到了。这里失败只意味着
    // 档案少填几项,用户可以在"我的"里补 —— 让注册在这时候报错,用户会以为自己
    // 没注册成功,然后再注册一次,拿到的是"这个邮箱已经注册过了"。
    let profile = toAccountProfile(result.user);
    try {
      const patched = await backend.updateMe({
        school: input.school.trim() || null,
        major: input.major.trim() || null,
        year: input.year.trim() || null,
        targetYear: input.targetYear || null,
        targetGoal: input.targetGoal.trim() || null,
      });
      profile = toAccountProfile(patched);
    } catch {
      // 静默:见上面的理由。用户能看到的仍然是"注册成功了"。
    }

    setUser(profile);
    return profile;
  }, []);

  const updateProfile = useCallback(
    async (patch: EditableProfile) => {
      if (!userRef.current) return;
      const updated = await backend.updateMe({
        displayName: patch.name,
        school: patch.school,
        major: patch.major,
        year: patch.year,
        rank: patch.rank,
        targetYear: patch.targetYear,
        targetGoal: patch.targetGoal,
        bio: patch.bio,
      });
      setUser(toAccountProfile(updated));
    },
    [],
  );

  const logout = useCallback(async () => {
    // 先调接口再清本地。顺序反过来的话,请求会因为拿不到令牌而登出失败 ——
    // 服务端那条会话就留着了,直到 30 天滑动过期。
    try {
      await backend.logout();
    } catch (error) {
      // 后端连不上时本地照样退出。**但要说清楚**:服务端那条会话还没撤销。
      if (!(error instanceof ApiError)) throw error;
    } finally {
      clearToken();
      setUser(null);
    }
  }, []);

  return useMemo(
    () => ({ ready, user, restoreProblem, retryRestore: restore, login, register, updateProfile, logout }),
    [ready, user, restoreProblem, restore, login, register, updateProfile, logout],
  );
}

const AuthContext = createContext<ReturnType<typeof useAuthState> | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const value = useAuthState();
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error('AuthProvider missing');
  return context;
}
