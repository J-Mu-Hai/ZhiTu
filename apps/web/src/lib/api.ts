/**
 * 后端接口客户端。
 *
 * ## 为什么这个文件必须存在,而不是在每个组件里各写一次 fetch
 *
 * 之前整个前端只有两处 `fetch`,都写在 provider.tsx 里,而且两处都**没有**
 * `Authorization` 头 —— 后端当时是"没有头就当成 dev_user",所以看起来能用。
 * 一旦后端开始认真鉴权,每写一个新的 fetch 就必须记得带上令牌,而"记得"是一种
 * 会失效的东西:漏一次就是一个静默的 401,表现成"页面空白"而不是报错。
 *
 * 所以令牌的附加、错误的翻译、401 的统一处理都收在这里。调用方只写路径和类型。
 *
 * ## 错误一律是 ApiError
 *
 * 原来的 `catch {}` 把失败变成了"看起来还能用"。现在每一次失败都带着后端给的
 * `code` 和中文 `message` 回到调用方,由界面决定怎么显示 —— 但**不能**再被吞掉。
 */

export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://127.0.0.1:8000'
).replace(/\/+$/, '');

/** 令牌只存在这里。键名带版本号,将来换存储方式时不会和旧值打架。 */
const TOKEN_KEY = 'zhitu.auth.token.v1';

/** 后端返回的错误体:{error: {code, message, details?}} */
interface ApiErrorBody {
  error?: { code?: string; message?: string; details?: unknown };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /**
   * 重试有没有意义。
   *
   * 503(数据库不可用)与 429(限流)值得再试一次;401/403/404/422 再点一百次
   * 也是同样的结果。界面据此决定重试按钮是显示还是灰掉 —— 让用户点一个注定失败的
   * 按钮,比不给他按钮更糟。
   */
  get retryable(): boolean {
    return this.status >= 500 || this.status === 429 || this.status === 408;
  }
}

// ---------------------------------------------------------------------------------
// 令牌
// ---------------------------------------------------------------------------------

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/**
 * 令牌失效时的回调。由 AuthProvider 注册 —— 这样"401 了要退出登录"这件事
 * 不必写在每一个调用点。
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

// ---------------------------------------------------------------------------------
// 请求
// ---------------------------------------------------------------------------------

interface RequestOptions {
  // `PUT` 只有布局那一条用它,而且它是**幂等的整份提交** —— 这正是 PUT 的语义
  // ("把我看到的那一份存成现在这样"),不是 PATCH 的逐条修改。
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  /** 关掉 401 的全局处理。登录接口自己需要:密码错就是密码错,不该把别的会话踢掉。 */
  skipUnauthorizedHandler?: boolean;
  signal?: AbortSignal;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipUnauthorizedHandler, signal } = options;

  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') throw error;
    // 连不上后端。给出的是"服务没起来"这种可操作的判断,而不是一个 TypeError。
    throw new ApiError(0, 'NETWORK_UNREACHABLE', `连不上后端服务(${API_BASE})。请确认后端已经启动。`);
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }

  if (!response.ok) {
    const envelope = (parsed ?? {}) as ApiErrorBody;
    const code = envelope.error?.code ?? `HTTP_${response.status}`;
    const message = envelope.error?.message ?? `请求失败(${response.status})。`;
    if (response.status === 401 && !skipUnauthorizedHandler) onUnauthorized?.();
    throw new ApiError(response.status, code, message, envelope.error?.details);
  }

  return parsed as T;
}
