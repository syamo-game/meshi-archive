export class AppError extends Error {
  constructor(public readonly status: 400 | 401 | 403 | 404 | 409 | 413 | 422 | 429 | 502 | 503 | 504, message: string) {
    super(message);
  }
}

export function failureMessage(error: object | string | number | boolean | null | undefined): string {
  return error instanceof Error ? error.message : String(error);
}
