import { afterEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';
import { setCsrfToken } from './csrf';
import { ApiError, http, onUnauthorized } from './http';

const respond = (body: unknown, init: ResponseInit = {}) =>
  new Response(body === null ? null : JSON.stringify(body), init);

const mockFetch = (res: Response | (() => never)) => {
  const fn = vi.fn(async () => (typeof res === 'function' ? res() : res));
  vi.stubGlobal('fetch', fn);
  return fn;
};

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken(null);
  document.cookie = 'ci_csrf=; Max-Age=0';
});

describe('cliente HTTP', () => {
  it('envia cookies de sessao (credentials: include) e valida a resposta com zod', async () => {
    const fetchMock = mockFetch(respond({ ok: true }, { status: 200 }));
    const data = await http('/status', { schema: z.object({ ok: z.boolean() }) });
    expect(data).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/status');
    expect(init.credentials).toBe('include');
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBeUndefined();
  });

  it('mutacoes levam X-CSRF-Token (memoria)', async () => {
    setCsrfToken('abc');
    const fetchMock = mockFetch(respond(null, { status: 204 }));
    await http('/control/stop-all', { method: 'POST', body: { a: 1 } });
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('abc');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('prefere o cookie CSRF legivel (double-submit)', async () => {
    setCsrfToken('memoria');
    document.cookie = 'ci_csrf=do-cookie';
    const fetchMock = mockFetch(respond(null, { status: 204 }));
    await http('/auth/logout', { method: 'POST' });
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('do-cookie');
  });

  it('GET nunca envia o token CSRF', async () => {
    setCsrfToken('abc');
    const fetchMock = mockFetch(respond({}, { status: 200 }));
    await http('/status');
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBeUndefined();
  });

  it('erro RFC 7807 vira ApiError com title/detail', async () => {
    mockFetch(
      respond(
        {
          type: 'about:blank',
          title: 'Dados inválidos',
          status: 422,
          detail: 'Confira os campos.',
          errors: [{ loc: ['body', 'risco'], message: 'máx 1%' }],
        },
        { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
      ),
    );
    const error = await http('/x', { method: 'POST', body: {} }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const api = error as ApiError;
    expect(api.status).toBe(422);
    expect(api.problem.title).toBe('Dados inválidos');
    expect(api.fieldMessage('risco')).toBe('máx 1%');
    expect(api.message).toBe('Confira os campos.');
  });

  it('corpo de erro nao-JSON ainda gera ApiError', async () => {
    mockFetch(new Response('<html>', { status: 502, statusText: 'Bad Gateway' }));
    const api = (await http('/x').catch((e: unknown) => e)) as ApiError;
    expect(api.status).toBe(502);
    expect(api.problem.title).toBe('Bad Gateway');
  });

  it('falha de rede vira ApiError de status 0 com mensagem amigavel', async () => {
    mockFetch(() => {
      throw new TypeError('Failed to fetch');
    });
    const api = (await http('/x').catch((e: unknown) => e)) as ApiError;
    expect(api.isNetwork).toBe(true);
    expect(api.message).toContain('Verifique a internet');
  });

  it('401 avisa a sessao expirada, exceto no modo silencioso', async () => {
    const listener = vi.fn();
    const off = onUnauthorized(listener);
    mockFetch(respond({ title: 'x' }, { status: 401 }));
    await http('/x', { silent401: true }).catch(() => undefined);
    expect(listener).not.toHaveBeenCalled();
    await http('/x').catch(() => undefined);
    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });

  it('resposta fora do contrato e rejeitada', async () => {
    mockFetch(respond({ ok: 'sim' }, { status: 200 }));
    const api = (await http('/x', { schema: z.object({ ok: z.boolean() }) }).catch(
      (e: unknown) => e,
    )) as ApiError;
    expect(api.problem.title).toBe('Resposta inesperada');
  });
});
