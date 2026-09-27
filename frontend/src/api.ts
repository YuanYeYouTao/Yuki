export type Row = Record<string, unknown>;
export interface Page {
  items: Row[];
  next_cursor: string | null;
  snapshot_at: string | null;
}
export interface Method {
  name: string;
  kind: "query" | "command";
  capability: string;
  authorized: boolean;
}
export interface Session {
  csrf: string;
  content_access: { chat: boolean };
  surface: { protocol_version: string; methods: Method[] };
}
export interface Command {
  request_id: string;
  expected_revision: number;
  payload: Row;
  target: Row;
}
const descriptions: Record<string, string> = {
  unauthenticated: "登录已失效，请重新登录。",
  capability_denied: "当前账号没有此操作权限。",
  version_conflict: "资料已被更新。请刷新后检查最新版本，再提交。",
  idempotency_conflict: "这个请求编号已有另一份内容，请查询原操作。",
  state_mismatch: "持久记录与当前状态不一致，请检查详情。",
  validation_error: "输入不符合接口要求，请检查格式和必填项。",
  not_found: "记录不存在、已过期，或不属于指定范围。",
  operation_unavailable: "服务暂不可用，请查看健康状态。",
  precondition_failed: "当前状态不允许此操作，请刷新查看。",
  secret_not_readable: "凭据不能读取，只能替换。",
  transport_unknown:
    "没有收到回执，操作结果未知。请查询原请求，勿重复创建操作。",
  transport_unavailable: "读取中断，请刷新后重试。",
  invalid_response: "服务返回的内容无法读取，请检查服务状态。",
};
export class ApiError extends Error {
  code: string;
  requestId?: string;
  constructor(code: string, requestId?: string) {
    super(descriptions[code] || `操作未完成：${code}`);
    this.code = code;
    this.requestId = requestId;
  }
}
let csrf = "";
export async function request<T>(
  path: string,
  body?: unknown,
  requestId?: string,
  signal?: AbortSignal,
  mutation = false,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/control/${path}`, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      signal,
      headers: {
        ...(body !== undefined
          ? { "Content-Type": "application/json", "X-Yuki-CSRF": csrf }
          : {}),
        ...(requestId ? { "X-Request-ID": requestId } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(
      mutation ? "transport_unknown" : "transport_unavailable",
      requestId,
    );
  }
  let value: Row;
  try {
    value = await response.json();
  } catch {
    throw new ApiError(
      mutation ? "transport_unknown" : "invalid_response",
      requestId,
    );
  }
  if (!response.ok || value.problem) {
    const problem = value.problem as Row | undefined;
    const error = new ApiError(
      String(
        problem?.code ||
          (mutation && response.status >= 500
            ? "transport_unknown"
            : `http_${response.status}`),
      ),
      String(value.request_id || requestId || ""),
    );
    if (response.status === 401) {
      csrf = "";
      window.dispatchEvent(new Event("session-expired"));
    }
    throw error;
  }
  return ("data" in value ? value.data : value) as T;
}
export async function login(credential: string) {
  await request("login", { credential });
  return restoreSession();
}
export async function restoreSession(): Promise<Session> {
  const session = await request<Session>("session");
  csrf = session.csrf;
  return session;
}
export async function logout() {
  await request("logout", {});
  csrf = "";
}
export function query<T>(name: string, args: Row = {}, signal?: AbortSignal) {
  return request<T>(
    `queries/${encodeURIComponent(name)}`,
    args,
    crypto.randomUUID(),
    signal,
  );
}
export function command(name: string, envelope: Command) {
  return request<Row>(
    `commands/${encodeURIComponent(name)}`,
    envelope,
    envelope.request_id,
    undefined,
    true,
  );
}
