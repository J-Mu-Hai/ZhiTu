'use client';

import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import type { AccountProfile, EditableProfile, RegisterAccountInput } from './types';

const ACCOUNTS_KEY = 'zhitu.auth.accounts.v1';
const SESSION_KEY = 'zhitu.auth.session.v1';

function readAccounts(): AccountProfile[] {
  try {
    const value = localStorage.getItem(ACCOUNTS_KEY);
    if (!value) return [];
    const accounts = JSON.parse(value) as AccountProfile[];
    return Array.isArray(accounts) ? accounts : [];
  } catch {
    return [];
  }
}

function writeAccounts(accounts: AccountProfile[]) {
  localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(accounts));
}

async function passwordDigest(accountId: string, password: string) {
  const data = new TextEncoder().encode(`${accountId}:${password}`);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

function normalizeEmail(email: string) {
  return email.trim().toLowerCase();
}

function useAuthState() {
  const [ready, setReady] = useState(false);
  const [user, setUser] = useState<AccountProfile | null>(null);

  useEffect(() => {
    const sessionId = localStorage.getItem(SESSION_KEY);
    const account = sessionId ? readAccounts().find((item) => item.id === sessionId) ?? null : null;
    if (sessionId && !account) localStorage.removeItem(SESSION_KEY);
    setUser(account);
    setReady(true);
  }, []);

  async function login(email: string, password: string) {
    const account = readAccounts().find((item) => item.email === normalizeEmail(email));
    if (!account) throw new Error('没有找到这个账户，请检查邮箱或先注册。');
    const digest = await passwordDigest(account.id, password);
    if (account.passwordDigest !== digest) throw new Error('密码不正确，请重新输入。');
    localStorage.setItem(SESSION_KEY, account.id);
    setUser(account);
    return account;
  }

  async function register(input: RegisterAccountInput) {
    const accounts = readAccounts();
    const email = normalizeEmail(input.email);
    if (accounts.some((item) => item.email === email)) throw new Error('这个邮箱已经注册，可以直接登录。');
    if (input.password.length < 8) throw new Error('密码至少需要 8 个字符。');
    const id = crypto.randomUUID();
    const account: AccountProfile = {
      id,
      name: input.name.trim(),
      email,
      passwordDigest: await passwordDigest(id, input.password),
      school: input.school.trim(),
      major: input.major.trim(),
      year: input.year.trim() || '大一',
      rank: 0,
      targetYear: input.targetYear,
      targetGoal: input.targetGoal.trim(),
      bio: '把遥远的目标，变成今天可以迈出的一小步。',
      createdAt: new Date().toISOString(),
    };
    writeAccounts([...accounts, account]);
    localStorage.setItem(SESSION_KEY, account.id);
    setUser(account);
    return account;
  }

  function updateProfile(profile: EditableProfile) {
    if (!user) return;
    const updated = { ...user, ...profile };
    writeAccounts(readAccounts().map((account) => (account.id === user.id ? updated : account)));
    setUser(updated);
  }

  function logout() {
    localStorage.removeItem(SESSION_KEY);
    setUser(null);
  }

  return { ready, user, login, register, updateProfile, logout };
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
