import { QueryClient, useQuery, useQueryClient } from '@tanstack/react-query';
import type { z } from 'zod';
import { ApiError, http } from './http';
import {
  accountSchema,
  botStatusSchema,
  commandAcceptedSchema,
  commandPageSchema,
  credentialSchema,
  credentialsSchema,
  decisionPageSchema,
  equitySchema,
  liveGateSchema,
  onboardingSchema,
  positionsSchema,
  powerSchema,
  recoveryCodesSchema,
  riskLimitsSchema,
  sessionListSchema,
  settingsSchema,
  settingsValidationSchema,
  startPaperSchema,
} from './schemas';

export const KEYS = {
  status: ['status'],
  limits: ['limits'],
  account: ['account'],
  equity: ['equity'],
  decisions: ['decisions'],
  positions: ['positions'],
  liveGate: ['live-gate'],
  commands: ['commands'],
  onboarding: ['onboarding'],
  credentials: ['credentials'],
  settings: ['settings'],
  sessions: ['sessions'],
  candles: ['candles'],
  trades: ['trades'],
  performance: ['performance'],
} as const;
export const STATUS_KEY = KEYS.status;
export const LIMITS_KEY = KEYS.limits;

export const createQueryClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5_000,
        refetchOnWindowFocus: false,
        retry: (count, error) =>
          !(error instanceof ApiError && error.status < 500 && error.status > 0) && count < 2,
      },
    },
  });

const read = <T>(
  key: readonly unknown[],
  path: string,
  schema: z.ZodType<T>,
  refetch?: number,
) => ({
  queryKey: key,
  queryFn: ({ signal }: { signal: AbortSignal }) => http(path, { schema, signal }),
  ...(refetch ? { refetchInterval: refetch } : {}),
});

export const useBotStatus = () =>
  useQuery({ ...read(KEYS.status, '/status', botStatusSchema, 30_000), staleTime: 2_000 });
export const useRiskLimits = () => useQuery(read(KEYS.limits, '/risk/limits', riskLimitsSchema));
export const useAccount = () => useQuery(read(KEYS.account, '/account/summary', accountSchema));
export const useEquity = (days = 30) =>
  useQuery(read([...KEYS.equity, days], `/equity?days=${days}&points=120`, equitySchema));
export const useDecisions = (limit = 5) =>
  useQuery(read([...KEYS.decisions, limit], `/decisions?limit=${limit}`, decisionPageSchema));
export const usePositions = () => useQuery(read(KEYS.positions, '/positions', positionsSchema));
export const useLiveGate = () => useQuery(read(KEYS.liveGate, '/live-gate', liveGateSchema));
export const useCommands = (limit = 10) =>
  useQuery({
    ...read([...KEYS.commands, limit], `/control/commands?limit=${limit}`, commandPageSchema),
    refetchInterval: (q: { state: { data?: z.infer<typeof commandPageSchema> } }) =>
      q.state.data?.items.some((c) => c.state === 'pending_delivery' || c.state === 'delivered')
        ? 2_000
        : false,
  });
export const useOnboarding = () => useQuery(read(KEYS.onboarding, '/onboarding', onboardingSchema));
export const useCredentials = (enabled = true) =>
  useQuery({ ...read(KEYS.credentials, '/credentials', credentialsSchema), enabled });
export const useSettings = () => useQuery(read(KEYS.settings, '/settings', settingsSchema));
export const useSessions = () => useQuery(read(KEYS.sessions, '/sessions', sessionListSchema));

export interface OperatorBody {
  operator_name: string | null;
  reason: string;
}

const post = <T>(path: string, body: unknown, schema: z.ZodType<T>) =>
  http(path, { method: 'POST', body, schema });

export const controlApi = {
  pause: (b: OperatorBody) => post('/control/pause', b, commandAcceptedSchema),
  resume: (b: OperatorBody) => post('/control/resume', b, commandAcceptedSchema),
  reactivate: (b: OperatorBody) => post('/control/reactivate', b, commandAcceptedSchema),
  emergencyStop: (b: { liquidate: boolean; operator_name: string | null; reason: string | null }) =>
    post('/control/emergency-stop', b, commandAcceptedSchema),
  power: (b: { on: boolean; operator_name: string | null; reason: string }) =>
    post('/control/power', b, powerSchema),
  activateLive: (b: { phrase: string | null; code: string | null }) =>
    http('/live/activate', { method: 'POST', body: b }),
};

export const accountApi = {
  changePassword: (b: { current_password: string; new_password: string }) =>
    http('/auth/password', { method: 'POST', body: b }),
  newRecoveryCodes: () => post('/auth/2fa/recovery-codes', undefined, recoveryCodesSchema),
  revokeSession: (id: string) => http(`/sessions/${id}`, { method: 'DELETE' }),
  revokeOtherSessions: () => http('/sessions', { method: 'DELETE' }),
};

export const onboardingApi = {
  saveCredential: (b: { label: string; api_key: string; api_secret: string }) =>
    post('/credentials', { exchange: 'binance', ...b }, credentialSchema),
  validateRisk: (value: Record<string, string>) =>
    post('/settings/validate', { section: 'risk', value }, settingsValidationSchema),
  saveRisk: (b: { value: Record<string, string>; reason: string; expected_version: number }) =>
    http('/settings/risk', {
      method: 'PUT',
      body: { ...b, acknowledge_risk_increase: false },
    }),
  startPaper: () => post('/onboarding/start-paper', undefined, startPaperSchema),
};

/** Depois de um comando ao robo, o estado e os comandos mudam: revalida o que a tela mostra. */
export function useInvalidateAfterCommand() {
  const qc = useQueryClient();
  return () =>
    Promise.all(
      [KEYS.status, KEYS.limits, KEYS.commands, KEYS.onboarding].map((k) =>
        qc.invalidateQueries({ queryKey: k }),
      ),
    );
}
