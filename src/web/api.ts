export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const readOnly = !options.method || options.method === 'GET';
  for (let attempt = 0; attempt < (readOnly ? 2 : 1); attempt++) {
    let response: Response;
    try {
      response = await fetch('/api' + path, {
        ...options,
        headers: { 'Content-Type': 'application/json', ...options.headers },
      });
    } catch {
      if (readOnly && attempt === 0) continue;
      throw new ApiError('连接中断，请查看记录确认结果，避免重复寄出', 0);
    }
    if (readOnly && attempt === 0 && response.status >= 500) {
      await response.body?.cancel();
      continue;
    }
    let data: unknown;
    try {
      data = await response.json();
    } catch {
      if (readOnly && attempt === 0) continue;
      throw new ApiError('服务响应未能确认，请先查看记录', 0);
    }
    if (!response.ok)
      throw new ApiError((data as { error?: string }).error || '操作未完成', response.status);
    return data as T;
  }
  throw new ApiError('暂时无法连接邮局，请稍后再试', 0);
}
export const post = <T>(path: string, body: unknown = {}) =>
  api<T>(path, { method: 'POST', body: JSON.stringify(body) });
