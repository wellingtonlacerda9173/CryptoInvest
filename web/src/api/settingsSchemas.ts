import { z } from 'zod';

const str = z.string();
const num = z.number();
const nnum = z.number().nullable();

export const SECTIONS = ['pairs', 'strategies', 'risk', 'ai'] as const;
export type SectionName = (typeof SECTIONS)[number];
export type SectionValue = Record<string, unknown>;

export const fieldChangeSchema = z.object({
  path: str,
  label: str,
  before_text: str,
  after_text: str,
  effect: z.enum(['increase', 'decrease', 'neutral', 'unknown']),
  increases_risk: z.boolean(),
  cost_text: str,
  applies: z.enum(['now', 'next_start']),
});
export type FieldChange = z.infer<typeof fieldChangeSchema>;

export const APPLICATIONS = [
  'none',
  'pending',
  'applied',
  'partially_applied',
  'refused',
  'expired',
  'failed',
] as const;

export const settingsVersionSchema = z.looseObject({
  id: num,
  created_at: str,
  actor: str,
  reason: str,
  sections: z.array(str),
  activation: z.enum(['immediate', 'next_start']),
  risk_increase: z.boolean(),
  changes: z.array(fieldChangeSchema),
  application: z.enum(APPLICATIONS),
  application_text: str,
});
export type SettingsVersion = z.infer<typeof settingsVersionSchema>;

export const fieldSpecSchema = z.object({
  path: str,
  label: str,
  kind: z.enum(['number', 'integer', 'boolean', 'text', 'list', 'choice']),
  default: z.unknown(),
  minimum: nnum.optional(),
  maximum: nnum.optional(),
  hard_limit: z.boolean(),
  unit: z.enum(['%', '']),
  choices: z.array(str).nullable().optional(),
});
export type FieldSpec = z.infer<typeof fieldSpecSchema>;

const sectionDoc = z.record(z.string(), z.unknown());
const document = z.looseObject({
  pairs: sectionDoc,
  strategies: sectionDoc,
  risk: sectionDoc,
  ai: sectionDoc,
});

export const fullSettingsSchema = z.looseObject({
  version: settingsVersionSchema.nullable(),
  document,
  defaults: document,
  fields: z.looseObject({
    pairs: z.array(fieldSpecSchema),
    strategies: z.array(fieldSpecSchema),
    risk: z.array(fieldSpecSchema),
    ai: z.array(fieldSpecSchema),
  }),
  hard_limits: z.array(
    z.object({ path: str, label: str, kind: z.enum(['max', 'min']), value: num, text: str }),
  ),
  effective: z.looseObject({
    runner_connected: z.boolean(),
    matches_latest: z.boolean().nullable(),
    restart_rule: str,
  }),
  can_edit: z.boolean(),
  capital_reference: num,
});
export type FullSettings = z.infer<typeof fullSettingsSchema>;

export const validationSchema = z.object({
  valid: z.boolean(),
  errors: z.array(z.object({ path: str, message: str })),
  changes: z.array(fieldChangeSchema),
  increases_risk: z.boolean(),
  requires_acknowledgement: z.boolean(),
  activation: z.enum(['immediate', 'next_start', 'none']),
  activation_text: str,
});
export type Validation = z.infer<typeof validationSchema>;

export const updateResultSchema = z.looseObject({
  version: settingsVersionSchema,
  activation: z.enum(['immediate', 'next_start', 'none']),
  activation_text: str,
  applied_now: z.array(str),
  pending_restart: z.array(str),
});
export type UpdateResult = z.infer<typeof updateResultSchema>;

export const historyPageSchema = z.object({
  items: z.array(settingsVersionSchema),
  next_cursor: z.number().nullable().optional(),
});
