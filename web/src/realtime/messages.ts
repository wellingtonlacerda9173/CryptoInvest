import { z } from 'zod';

export const WS_CHANNELS = [
  'status',
  'decisions',
  'orders',
  'trades',
  'equity',
  'alerts',
  'heartbeat',
] as const;
export type WsChannel = (typeof WS_CHANNELS)[number];

/** Contrato em api/ws_schema.json (servidor -> cliente). Mensagens fora dele sao ignoradas. */
export const wsMessageSchema = z.discriminatedUnion('type', [
  z.looseObject({
    type: z.literal('hello'),
    runner_connected: z.boolean(),
    ping_seconds: z.number().positive().optional(),
    role: z.enum(['admin', 'reader']),
  }),
  z.looseObject({ type: z.literal('subscribed') }),
  z.looseObject({
    type: z.literal('event'),
    id: z.string(),
    channel: z.enum(WS_CHANNELS),
    at: z.string(),
  }),
  z.looseObject({ type: z.literal('runner'), connected: z.boolean(), message: z.string() }),
  z.looseObject({ type: z.literal('resync'), reason: z.string() }),
  z.looseObject({ type: z.literal('ping') }),
  z.looseObject({ type: z.literal('error'), code: z.string(), message: z.string() }),
]);
export type WsMessage = z.infer<typeof wsMessageSchema>;
