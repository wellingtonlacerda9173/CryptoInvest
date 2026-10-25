import { z } from 'zod';
import type { Schemas } from './schemas';

type Typed<K extends keyof Schemas> = z.ZodType<Schemas[K]>;

const nstr = z.string().nullable();

const eventKind = z.enum(['fomc', 'cpi', 'payroll', 'deribit', 'other']).catch('other');

export const calendarSchema: Typed<'CalendarOut'> = z.object({
  state: z.enum(['ok', 'expiring', 'expired']),
  now: z.string(),
  loaded_at: nstr.optional(),
  last_success_at: nstr.optional(),
  valid_until: nstr.optional(),
  days_remaining: z.number().nullable().optional(),
  margin_before_minutes: z.number(),
  margin_after_minutes: z.number(),
  upcoming: z.array(
    z.object({
      start: z.string(),
      end: z.string(),
      label: z.string(),
      kind: eventKind,
      source: z.string(),
      block_start: z.string(),
      block_end: z.string(),
    }),
  ),
  last_update: z.object({
    attempted_at: nstr.optional(),
    result: z.enum(['updated', 'unchanged', 'rejected', 'failed', 'never']),
  }),
});

export type Calendar = Schemas['CalendarOut'];
export type CalendarEvent = Schemas['CalendarEventOut'];
