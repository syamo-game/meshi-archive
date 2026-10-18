import { z } from 'zod';
export * from '@meshi/contracts';

const errorSchema = z.object({
  detail: z.union([z.string(), z.object({ message: z.string() }), z.array(z.object({ msg: z.string() }))]).optional(),
  error: z.string().optional(), errors: z.union([z.array(z.string()), z.record(z.string(), z.string())]).optional(),
});
let csrfToken = '';
export function setCsrf(token: string): void { csrfToken = token; }
export function csrf(): string { return csrfToken; }
export class ApiError extends Error {
  constructor(message: string, public readonly status: number) { super(message); }
}
export async function api<T>(url: string, schema: z.ZodType<T>, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set('Accept', 'application/json');
  if (init.method && init.method !== 'GET') headers.set('X-CSRF-Token', csrfToken);
  const response = await fetch(url, { ...init, headers, credentials: 'same-origin', redirect: 'error' });
  if (!response.headers.get('content-type')?.includes('application/json')) {
    console.error('API returned a non-JSON response', { url, status: response.status });
    throw new ApiError('応答を読み込めませんでした。画面を再読み込みしてください。', response.status);
  }
  const payload: z.infer<typeof errorSchema> | T = await response.json();
  if (!response.ok) {
    const parsed = errorSchema.safeParse(payload);
    let message = `処理に失敗しました（${response.status}）。`;
    if (parsed.success) {
      const detail = parsed.data.detail;
      if (typeof detail === 'string') message = detail;
      else if (Array.isArray(detail)) message = detail.map(item => item.msg).join('\n');
      else if (detail) message = detail.message;
      const errors = parsed.data.errors;
      if (errors) message += '\n' + (Array.isArray(errors) ? errors : Object.values(errors)).join('\n');
    }
    console.error('API request failed', { url, status: response.status, message });
    throw new ApiError(message, response.status);
  }
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    console.error('API response validation failed', { url, status: response.status, issues: parsed.error.issues.map(issue => ({ path: issue.path, message: issue.message })) });
    throw new ApiError('応答の形式が一致しません。管理者にお問い合わせください。', response.status);
  }
  return parsed.data;
}
export function jsonBody(value: object): RequestInit { return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) }; }
