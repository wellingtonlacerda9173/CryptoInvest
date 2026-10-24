import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { http } from './http';
import { KEYS } from './queries';
import {
  fullSettingsSchema,
  historyPageSchema,
  updateResultSchema,
  validationSchema,
  type SectionName,
  type SectionValue,
} from './settingsSchemas';

export const SETTINGS_FULL_KEY = [...KEYS.settings, 'full'] as const;
export const SETTINGS_HISTORY_KEY = [...KEYS.settings, 'history'] as const;

export const useFullSettings = () =>
  useQuery({
    queryKey: SETTINGS_FULL_KEY,
    queryFn: ({ signal }) => http('/settings', { schema: fullSettingsSchema, signal }),
    staleTime: 0,
  });

export const useSettingsHistory = () =>
  useInfiniteQuery({
    queryKey: SETTINGS_HISTORY_KEY,
    queryFn: ({ signal, pageParam }) =>
      http(`/settings/history?limit=10${pageParam ? `&cursor=${pageParam}` : ''}`, {
        schema: historyPageSchema,
        signal,
      }),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    // o estado de aplicacao ao robo muda sozinho (pendente -> aplicado): revalida de tempos em tempos
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((v) => v.application === 'pending'))
        ? 3_000
        : false,
  });

export const settingsApi = {
  validate: (section: SectionName, value: SectionValue, signal?: AbortSignal) =>
    http('/settings/validate', {
      method: 'POST',
      body: { section, value },
      schema: validationSchema,
      signal,
    }),
  save: (
    section: SectionName,
    b: {
      value: SectionValue;
      reason: string;
      expected_version: number;
      acknowledge_risk_increase: boolean;
    },
  ) => http(`/settings/${section}`, { method: 'PUT', body: b, schema: updateResultSchema }),
};

export const credentialsApi = {
  create: (b: { label: string; api_key: string; api_secret: string }) =>
    http('/credentials', { method: 'POST', body: { exchange: 'binance', ...b } }),
  rotate: (id: string, b: { api_key: string; api_secret: string }) =>
    http(`/credentials/${id}`, { method: 'PUT', body: b }),
  remove: (id: string) => http(`/credentials/${id}`, { method: 'DELETE' }),
  verify: (id: string) => http(`/credentials/${id}/verify`, { method: 'POST' }),
};
