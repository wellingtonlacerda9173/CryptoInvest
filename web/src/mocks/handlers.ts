import { delay, http as rest, HttpResponse } from 'msw';
import { mockControls } from './fakeSocket';
import { extraHandlers } from './handlersExtra';
import {
  getMockStatus,
  makeAccount,
  makeCommand,
  makeCredential,
  makeDecisions,
  makeEquity,
  makeGate,
  makeOnboarding,
  makePositions,
  makeSessions,
  MOCK_ACTIVATION,
  MOCK_CSRF,
  MOCK_PASSWORD,
  MOCK_RECOVERY,
  MOCK_TOTP,
  MOCK_USERS,
  patchMockStatus,
} from './mockState';

const SESSION = 'ci-mock-session';
const PENDING = 'ci-mock-pending';
const CREDENTIAL = 'ci-mock-credential';

const problem = (status: number, title: string, detail: string, type = 'about:blank') =>
  HttpResponse.json(
    { type, title, status, detail },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  );

const store = {
  get: (k: string) => {
    try {
      return sessionStorage.getItem(k);
    } catch {
      return null;
    }
  },
  set: (k: string, v: string | null) => {
    try {
      if (v === null) sessionStorage.removeItem(k);
      else sessionStorage.setItem(k, v);
    } catch {
      // ignora
    }
  },
};

const userOf = (key: string | null) =>
  key === 'admin' ? MOCK_USERS.admin : key === 'reader' ? MOCK_USERS.reader : null;
const unauthenticated = () => problem(401, 'Não autenticado', 'Entre para continuar.');
const userRow = (u: (typeof MOCK_USERS)[keyof typeof MOCK_USERS]) => ({
  ...u,
  totp_enabled: true,
  disabled: false,
  created_at: new Date().toISOString(),
});

let freshUntil = 0;
const fresh = () => Date.now() < freshUntil;
const needFresh = () =>
  problem(
    403,
    'Confirme sua identidade',
    'Confirme sua identidade com o codigo do app autenticador.',
    'https://cryptoinvest.local/problems/fresh-auth-required',
  );

/** Leitura autenticada. */
const authed =
  <T>(fn: () => T) =>
  () =>
    store.get(SESSION) ? HttpResponse.json(fn() as never) : unauthenticated();

const commandLog = [makeCommand()];
const sendCommand = (name: Parameters<typeof makeCommand>[0] & object, state = 'applied') => {
  const command = makeCommand({
    id: `cmd-${commandLog.length + 1}`,
    created_at: new Date().toISOString(),
    state: state as 'applied',
    ...name,
  });
  commandLog.unshift(command);
  return command;
};

const adminOnly = (request: Request) => {
  if (!store.get(SESSION)) return unauthenticated();
  if (store.get(SESSION) !== 'admin')
    return problem(403, 'Sem permissão', 'Somente administradores.');
  if (request.headers.get('X-CSRF-Token') !== MOCK_CSRF)
    return problem(403, 'CSRF', 'Token ausente.');
  return null;
};

export const handlers = [
  ...extraHandlers({ authed, adminOnly, fresh, needFresh, problem }),
  rest.get('/api/auth/me', () => {
    const user = userOf(store.get(SESSION));
    if (!user) return unauthenticated();
    return HttpResponse.json({
      id: user.id,
      username: user.username,
      role: user.role,
      totp_enabled: true,
      csrf_token: MOCK_CSRF,
      fresh_auth_until: fresh() ? new Date(freshUntil).toISOString() : null,
    });
  }),

  rest.post('/api/auth/login', async ({ request }) => {
    await delay(150);
    const body = (await request.json()) as { username?: string; password?: string };
    const entry = Object.entries(MOCK_USERS).find(([, u]) => u.username === body.username);
    if (body.username === 'novo.usuario' && body.password === MOCK_PASSWORD) {
      store.set(PENDING, 'admin');
      return HttpResponse.json({
        stage: 'enrollment',
        csrf_token: MOCK_CSRF,
        expires_in_seconds: 900,
      });
    }
    if (!entry || body.password !== MOCK_PASSWORD) {
      return problem(401, 'Credenciais inválidas', 'Usuário, senha ou código não conferem.');
    }
    store.set(PENDING, entry[0]);
    return HttpResponse.json({ stage: 'totp', csrf_token: MOCK_CSRF, expires_in_seconds: 300 });
  }),

  rest.post('/api/auth/2fa/verify', async ({ request }) => {
    await delay(150);
    if (request.headers.get('X-CSRF-Token') !== MOCK_CSRF)
      return problem(403, 'CSRF', 'Token ausente.');
    const pending = store.get(PENDING);
    const body = (await request.json()) as { code?: string; recovery_code?: string };
    const user = userOf(pending);
    const ok =
      body.code === MOCK_TOTP || (body.recovery_code && MOCK_RECOVERY.includes(body.recovery_code));
    if (!user || !ok)
      return problem(401, 'Código inválido', 'Usuário, senha ou código não conferem.');
    store.set(PENDING, null);
    store.set(SESSION, pending);
    return HttpResponse.json({
      user: userRow(user),
      csrf_token: MOCK_CSRF,
      expires_at: new Date(Date.now() + 8 * 3600_000).toISOString(),
      recovery_code_used: !!body.recovery_code,
    });
  }),

  rest.post('/api/auth/2fa/enroll', async ({ request }) => {
    const body = (await request.json()) as { activation_token?: string };
    if (body.activation_token !== MOCK_ACTIVATION)
      return problem(401, 'Código inválido', 'Código de ativação inválido ou vencido.');
    return HttpResponse.json({
      otpauth_uri: 'otpauth://totp/CryptoInvest:novo?secret=JBSWY3DPEHPK3PXP',
      secret: 'JBSWY3DPEHPK3PXP',
      qr_svg_data_uri:
        "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><rect width='200' height='200' fill='white'/><rect x='20' y='20' width='60' height='60'/><rect x='120' y='20' width='60' height='60'/><rect x='20' y='120' width='60' height='60'/></svg>",
    });
  }),

  rest.post('/api/auth/2fa/enroll/confirm', async ({ request }) => {
    const body = (await request.json()) as { code?: string };
    if (body.code !== MOCK_TOTP) return problem(401, 'Código inválido', 'Código não confere.');
    store.set(SESSION, store.get(PENDING));
    return HttpResponse.json({
      user: userRow(MOCK_USERS.admin),
      csrf_token: MOCK_CSRF,
      expires_at: new Date(Date.now() + 8 * 3600_000).toISOString(),
      recovery_codes: MOCK_RECOVERY,
    });
  }),

  rest.post('/api/auth/reauth', async ({ request }) => {
    const body = (await request.json()) as { code?: string };
    const user = userOf(store.get(SESSION));
    if (!user) return unauthenticated();
    if (body.code !== MOCK_TOTP) return problem(401, 'Código inválido', 'Código não confere.');
    freshUntil = Date.now() + 5 * 60_000;
    return HttpResponse.json({
      id: user.id,
      username: user.username,
      role: user.role,
      totp_enabled: true,
      csrf_token: MOCK_CSRF,
      fresh_auth_until: new Date(freshUntil).toISOString(),
    });
  }),

  rest.post('/api/auth/logout', () => {
    store.set(SESSION, null);
    return new HttpResponse(null, { status: 204 });
  }),

  rest.post('/api/auth/password', async ({ request }) => {
    if (!store.get(SESSION)) return unauthenticated();
    if (!fresh()) return needFresh();
    const body = (await request.json()) as { current_password?: string };
    if (body.current_password !== MOCK_PASSWORD)
      return problem(403, 'Senha inválida', 'A senha atual não confere.');
    return new HttpResponse(null, { status: 204 });
  }),
  rest.post('/api/auth/2fa/recovery-codes', () =>
    !store.get(SESSION)
      ? unauthenticated()
      : fresh()
        ? HttpResponse.json({ recovery_codes: MOCK_RECOVERY })
        : needFresh(),
  ),
  rest.get('/api/sessions', authed(makeSessions)),
  rest.delete('/api/sessions/:id', () => new HttpResponse(null, { status: 204 })),
  rest.delete('/api/sessions', () => new HttpResponse(null, { status: 204 })),

  rest.get('/api/status', authed(getMockStatus)),
  rest.get(
    '/api/risk/limits',
    authed(() => getMockStatus().limits),
  ),
  rest.get('/api/account/summary', authed(makeAccount)),
  rest.get('/api/equity', authed(makeEquity)),
  rest.get('/api/decisions', authed(makeDecisions)),
  rest.get(
    '/api/positions',
    authed(() => (getMockStatus().open_positions.length ? makePositions() : [])),
  ),
  rest.get('/api/live-gate', authed(makeGate)),
  rest.get(
    '/api/control/commands',
    authed(() => ({ items: commandLog, next_cursor: null })),
  ),
  rest.get(
    '/api/onboarding',
    authed(() => makeOnboarding(store.get(CREDENTIAL) === 'done')),
  ),
  rest.get(
    '/api/credentials',
    authed(() => (store.get(CREDENTIAL) === 'key' ? [makeCredential()] : [])),
  ),
  rest.post('/api/credentials', async ({ request }) => {
    const denied = adminOnly(request);
    if (denied) return denied;
    if (!fresh()) return needFresh();
    store.set(CREDENTIAL, 'key');
    return HttpResponse.json(makeCredential(), { status: 201 });
  }),
  rest.post('/api/onboarding/start-paper', async ({ request }) => {
    const denied = adminOnly(request);
    if (denied) return denied;
    store.set(CREDENTIAL, 'done');
    return HttpResponse.json(
      { state: 'started', message: 'Robô iniciado.', config_version: 1 },
      { status: 202 },
    );
  }),

  rest.post('/api/control/pause', async ({ request }) => {
    const denied = adminOnly(request);
    if (denied) return denied;
    await delay(200);
    const reason = ((await request.json()) as { reason: string }).reason;
    patchMockStatus({
      state: 'PAUSED',
      paused: true,
      state_label: 'Pausado',
      pause_reasons: [{ at: new Date().toISOString(), operator: 'ana.silva', reason }],
    });
    mockControls.push();
    return HttpResponse.json(
      { ...sendCommand({ name: 'pause', reason }), message: 'ok' },
      { status: 202 },
    );
  }),
  rest.post('/api/control/resume', async ({ request }) => {
    const denied = adminOnly(request);
    if (denied) return denied;
    patchMockStatus({
      state: 'RUNNING',
      paused: false,
      state_label: 'Operando',
      pause_reasons: [],
    });
    mockControls.push();
    return HttpResponse.json(
      { ...sendCommand({ name: 'resume' }), message: 'ok' },
      { status: 202 },
    );
  }),
  rest.post('/api/control/reactivate', async ({ request }) => {
    const denied = adminOnly(request);
    if (denied) return denied;
    if (!fresh()) return needFresh();
    patchMockStatus({
      state: 'RUNNING',
      halted: false,
      state_label: 'Operando',
      halt_reason: null,
    });
    mockControls.push();
    return HttpResponse.json(
      { ...sendCommand({ name: 'reactivate' }), message: 'ok' },
      { status: 202 },
    );
  }),
  rest.post('/api/control/power', async ({ request }) => {
    const denied = adminOnly(request);
    if (denied) return denied;
    const on = ((await request.json()) as { on: boolean }).on;
    patchMockStatus(
      on
        ? { state: 'RUNNING', state_label: 'Operando' }
        : { state: 'STOPPED', state_label: 'Parado' },
    );
    mockControls.push();
    return HttpResponse.json(
      {
        state: 'commanded',
        message: on ? 'Pedido para ligar enviado.' : 'Pedido para desligar enviado.',
        command: null,
      },
      { status: 202 },
    );
  }),
  rest.post('/api/control/emergency-stop', async ({ request }) => {
    const denied = adminOnly(request);
    if (denied) return denied;
    const body = (await request.json()) as { liquidate?: boolean; reason?: string | null };
    await delay(200);
    patchMockStatus({
      state: 'PAUSED',
      paused: true,
      state_label: 'Pausado',
      pause_reasons: [
        {
          at: new Date().toISOString(),
          operator: 'ana.silva',
          reason: body.reason ?? 'parada manual',
        },
      ],
    });
    mockControls.push();
    return HttpResponse.json(
      { ...sendCommand({ name: 'emergency-stop', liquidate: !!body.liquidate }), message: 'ok' },
      { status: 202 },
    );
  }),
  rest.post('/api/live/activate', () => problem(409, 'Conflito', 'Live ainda não disponível.')),
];
