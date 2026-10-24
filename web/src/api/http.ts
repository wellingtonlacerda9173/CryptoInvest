import { z } from 'zod';
import { CSRF_HEADER, getCsrfToken, isMutation } from './csrf';

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '/api';

const problemSchema = z.looseObject({
  type: z.string().optional(),
  title: z.string().optional(),
  status: z.number().optional(),
  detail: z.string().optional(),
  instance: z.string().optional(),
  request_id: z.string().optional(),
  retry_after_seconds: z.number().optional(),
  errors: z
    .array(z.object({ loc: z.array(z.union([z.string(), z.number()])), message: z.string() }))
    .optional(),
});
export type ProblemDetails = z.infer<typeof problemSchema>;

export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetails;

  constructor(status: number, problem: ProblemDetails) {
    super(problem.detail ?? problem.title ?? 'Erro inesperado');
    this.name = 'ApiError';
    this.status = status;
    this.problem = problem;
  }

  get isNetwork() {
    return this.status === 0;
  }

  get needsFreshAuth() {
    return this.status === 403 && (this.problem.type ?? '').endsWith('/fresh-auth-required');
  }

  /** Mensagem do campo `loc` (ex.: ['body','value','risk_per_trade']) quando o servidor devolve erros por campo. */
  fieldMessage(field: string): string | undefined {
    return this.problem.errors?.find((e) => e.loc.includes(field))?.message;
  }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();
export const onUnauthorized = (fn: Listener) => {
  unauthorizedListeners.add(fn);
  return () => {
    unauthorizedListeners.delete(fn);
  };
};

export interface RequestOptions<T> {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  schema?: z.ZodType<T>;
  signal?: AbortSignal;
  /** Em 401, nao dispara o evento global de sessao expirada (ex.: checagem inicial da sessao). */
  silent401?: boolean;
}

async function toProblem(res: Response): Promise<ProblemDetails> {
  const fallback: ProblemDetails = { status: res.status, title: res.statusText || 'Erro' };
  try {
    const parsed = problemSchema.safeParse(await res.json());
    return parsed.success ? { ...fallback, ...parsed.data } : fallback;
  } catch {
    return fallback;
  }
}

export async function http<T = void>(path: string, options: RequestOptions<T> = {}): Promise<T> {
  const { method = 'GET', body, schema, signal, silent401 } = options;
  const headers: Record<string, string> = { Accept: 'application/json, application/problem+json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (isMutation(method)) {
    const csrf = getCsrfToken();
    if (csrf) headers[CSRF_HEADER] = csrf;
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      credentials: 'include',
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new ApiError(0, {
      title: 'Sem conexão',
      detail: 'Não conseguimos falar com o servidor. Verifique a internet.',
    });
  }

  if (!res.ok) {
    if (res.status === 401 && !silent401) unauthorizedListeners.forEach((fn) => fn());
    throw new ApiError(res.status, await toProblem(res));
  }
  if (res.status === 204 || !schema) return undefined as T;

  const parsed = schema.safeParse(await res.json());
  if (!parsed.success) {
    throw new ApiError(res.status, {
      title: 'Resposta inesperada',
      detail: 'O servidor respondeu em um formato que o painel não entende.',
    });
  }
  return parsed.data;
}
