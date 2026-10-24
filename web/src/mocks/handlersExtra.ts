import { http as rest, HttpResponse } from 'msw';
import type { FieldChange, SectionName, SettingsVersion } from '@/api/settingsSchemas';
import { makeJob, makeReport } from '@/test/backtestData';
import { makeCandles, makePerformance, makeTrade, makeTradePage, LOSS } from '@/test/marketData';
import {
  DECREASE_CHANGE,
  INCREASE_CHANGE,
  makeFullSettings,
  validation,
} from '@/test/settingsData';
import { getMockStatus } from './mockState';

type Guard = (request: Request) => Response | null;
export interface MockHelpers {
  authed: <T>(fn: () => T) => () => Response;
  adminOnly: Guard;
  fresh: () => boolean;
  needFresh: () => Response;
  problem: (status: number, title: string, detail: string, type?: string) => Response;
}

const RISK_UP = ['risk_per_trade', 'max_daily_loss', 'max_open_positions', 'max_drawdown'];
const HARD_MAX: Record<string, number> = {
  risk_per_trade: 0.01,
  max_daily_loss: 0.05,
  max_drawdown: 0.05,
};

/** Estado em memoria (reinicia no F5, como o resto dos dados ficticios). */
const INITIAL_VERSION = makeFullSettings().version;
const state = {
  document: makeFullSettings().document,
  history: [] as SettingsVersion[],
};
const jobs = new Map<
  string,
  { job: ReturnType<typeof makeJob>; startedAt: number; cancelled: boolean }
>();
const DURATION_MS = 7_000;

const jobOf = (id: string) => {
  const entry = jobs.get(id);
  if (!entry) return null;
  if (entry.cancelled)
    return { ...entry.job, status: 'cancelled', finished_at: new Date().toISOString() };
  const pct = Math.min(100, ((Date.now() - entry.startedAt) / DURATION_MS) * 100);
  if (pct >= 100)
    return {
      ...entry.job,
      status: 'succeeded',
      has_report: true,
      finished_at: new Date().toISOString(),
      progress: { stage: 'done', percent: 100, message: 'Concluído', symbol: null },
    };
  const stage = pct < 25 ? 'loading' : 'simulating';
  return {
    ...entry.job,
    status: 'running',
    progress: {
      stage,
      percent: Math.round(pct),
      message: stage === 'loading' ? 'Baixando as velas' : 'Simulando as operações',
      symbol: 'BTC/USDT',
    },
  };
};

const changesFor = (section: string, value: Record<string, unknown>): FieldChange[] => {
  if (section !== 'risk') return [];
  const base = state.document.risk;
  return RISK_UP.flatMap((name) => {
    const before = Number(base[name]);
    const after = Number(value[name]);
    if (!Number.isFinite(after) || after === before) return [];
    return [
      after > before
        ? { ...INCREASE_CHANGE, path: `risk.${name}` }
        : { ...DECREASE_CHANGE, path: `risk.${name}` },
    ];
  });
};

export function extraHandlers(h: MockHelpers) {
  const ok = (body: unknown, init?: ResponseInit) => HttpResponse.json(body as never, init);
  return [
    rest.get(
      '/api/market/candles',
      h.authed(() => makeCandles()),
    ),
    rest.get(
      '/api/trades',
      h.authed(() => makeTradePage([makeTrade(1), LOSS(2), makeTrade(3)])),
    ),
    rest.get(
      '/api/performance',
      h.authed(() => makePerformance()),
    ),

    rest.get(
      '/api/settings',
      h.authed(() => ({
        ...makeFullSettings({ can_edit: true }),
        document: state.document,
        version: state.history[0] ?? INITIAL_VERSION,
      })),
    ),
    rest.get(
      '/api/settings/history',
      h.authed(() => ({ items: state.history, next_cursor: null })),
    ),
    rest.post('/api/settings/validate', async ({ request }) => {
      const denied = h.adminOnly(request);
      if (denied) return denied;
      const body = (await request.json()) as {
        section: SectionName;
        value: Record<string, unknown>;
      };
      const errors = Object.entries(HARD_MAX).flatMap(([name, max]) =>
        body.section === 'risk' && Number(body.value[name]) > max
          ? [
              {
                path: `risk.${name}`,
                message: `O valor máximo permitido é ${max} (teto de segurança do servidor).`,
              },
            ]
          : [],
      );
      return ok(validation(errors.length ? [] : changesFor(body.section, body.value), errors));
    }),
    rest.put('/api/settings/:section', async ({ request, params }) => {
      const denied = h.adminOnly(request);
      if (denied) return denied;
      if (!h.fresh()) return h.needFresh();
      const section = String(params.section) as SectionName;
      const body = (await request.json()) as {
        value: Record<string, unknown>;
        reason: string;
        expected_version: number;
        acknowledge_risk_increase: boolean;
      };
      const current = state.history[0]?.id ?? INITIAL_VERSION?.id ?? 0;
      if (body.expected_version !== current)
        return h.problem(
          409,
          'Conflito',
          'A configuração mudou enquanto você editava. Recarregue e tente de novo.',
          'https://cryptoinvest.local/problems/conflict',
        );
      const changes = changesFor(section, body.value);
      if (changes.length === 0)
        return h.problem(409, 'Conflito', 'Nada para salvar: os valores são iguais aos atuais.');
      const increases = changes.some((c) => c.increases_risk);
      if (increases && !body.acknowledge_risk_increase)
        return h.problem(
          409,
          'Confirme o aumento de risco',
          'Esta mudança aumenta o risco e só vale no próximo início do robô. Confirme para salvar.',
          'https://cryptoinvest.local/problems/risk-increase-unconfirmed',
        );
      state.document = { ...state.document, [section]: body.value };
      const version: SettingsVersion = {
        id: current + 1,
        created_at: new Date().toISOString(),
        actor: 'ana.silva',
        reason: body.reason,
        sections: [section],
        activation: increases ? 'next_start' : 'immediate',
        risk_increase: increases,
        changes,
        application: increases ? 'none' : 'applied',
        application_text: increases
          ? 'Vale no próximo início do robô.'
          : 'A redução de risco já está valendo no robô.',
      };
      state.history.unshift(version);
      return ok({
        version,
        activation: version.activation,
        activation_text: increases ? 'Só vale no próximo início do robô.' : 'Vale agora.',
        applied_now: [],
        pending_restart: [],
        command: null,
      });
    }),

    rest.get(
      '/api/backtests',
      h.authed(() => ({
        items: [...jobs.keys()].reverse().flatMap((id) => {
          const j = jobOf(id);
          return j ? [j] : [];
        }),
        next_cursor: null,
      })),
    ),
    rest.post('/api/backtests', async ({ request }) => {
      const denied = h.adminOnly(request);
      if (denied) return denied;
      const body = (await request.json()) as Record<string, unknown>;
      const id = Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join(
        '',
      );
      const now = Date.now();
      const end = new Date(now).toISOString();
      const start = new Date(now - 365 * 86_400_000).toISOString();
      jobs.set(id, {
        job: makeJob({
          id,
          status: 'queued',
          created_at: end,
          params: { start, end, ...body },
        }),
        startedAt: now,
        cancelled: false,
      });
      return ok(jobOf(id), { status: 202 });
    }),
    rest.get('/api/backtests/:id', ({ params }) => {
      const job = jobOf(String(params.id));
      return job ? ok(job) : h.problem(404, 'Não encontrado', 'Backtest não encontrado.');
    }),
    rest.get('/api/backtests/:id/report', ({ params }) => {
      const job = jobOf(String(params.id));
      return job?.status === 'succeeded'
        ? ok({ ...makeReport(), job_id: String(params.id) })
        : h.problem(404, 'Não encontrado', 'Este backtest ainda não tem relatório.');
    }),
    rest.post('/api/backtests/:id/cancel', ({ request, params }) => {
      const denied = h.adminOnly(request);
      if (denied) return denied;
      const entry = jobs.get(String(params.id));
      if (entry) entry.cancelled = true;
      return ok(jobOf(String(params.id)));
    }),

    rest.put(
      '/api/credentials/:id',
      ({ request }) => h.adminOnly(request) ?? (h.fresh() ? ok({}) : h.needFresh()),
    ),
    rest.post('/api/credentials/:id/verify', ({ request }) => h.adminOnly(request) ?? ok({})),
    rest.delete('/api/credentials/:id', ({ request }) => {
      const denied = h.adminOnly(request);
      if (denied) return denied;
      if (!h.fresh()) return h.needFresh();
      return getMockStatus().state === 'RUNNING' || getMockStatus().state === 'PAUSED'
        ? h.problem(
            409,
            'Conflito',
            'Desligue o robô antes de remover a chave: com ele ligado a remoção é recusada.',
            'https://cryptoinvest.local/problems/conflict',
          )
        : new HttpResponse(null, { status: 204 });
    }),
  ];
}
