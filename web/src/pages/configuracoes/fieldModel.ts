import type { FieldSpec, SectionName, SectionValue } from '@/api/settingsSchemas';

/** `risk.risk_per_trade` -> `risk_per_trade`; `strategies.ema.fast_period` -> `ema.fast_period`. */
export const relPath = (section: SectionName, path: string) =>
  path.startsWith(`${section}.`) ? path.slice(section.length + 1) : path;

export function getAt(obj: unknown, path: string): unknown {
  return path
    .split('.')
    .reduce<unknown>(
      (cur, key) =>
        cur !== null && typeof cur === 'object' ? (cur as Record<string, unknown>)[key] : undefined,
      obj,
    );
}

export function setAt(obj: SectionValue, path: string, value: unknown): SectionValue {
  const [head, ...rest] = path.split('.');
  if (head === undefined) return obj;
  if (rest.length === 0) return { ...obj, [head]: value };
  const child = (obj[head] ?? {}) as SectionValue;
  return { ...obj, [head]: setAt(child, rest.join('.'), value) };
}

const isPercent = (spec: FieldSpec) => spec.unit === '%';
const clean = (n: number) => Number(n.toFixed(8));

export const parseNumber = (text: string): number =>
  text.trim() === '' ? NaN : Number(text.trim().replace(',', '.'));

/** Valor guardado (fracao, ex.: "0.005") -> texto que a pessoa edita (ex.: "0.5"). */
export function toInput(spec: FieldSpec, value: unknown): string {
  const n = typeof value === 'number' || typeof value === 'string' ? Number(value) : NaN;
  if (!Number.isFinite(n)) return '';
  return String(isPercent(spec) ? clean(n * 100) : n);
}

/**
 * Texto digitado -> valor no formato do documento. O tipo original e preservado: decimais viajam como
 * texto ("0.005"), floats e inteiros como numero. `undefined` quando o texto nao e um numero.
 */
export function fromInput(
  spec: FieldSpec,
  text: string,
  original: unknown,
): number | string | undefined {
  const n = parseNumber(text);
  if (!Number.isFinite(n)) return undefined;
  const value = isPercent(spec) ? clean(n / 100) : n;
  return typeof original === 'string' ? String(value) : value;
}

export const isNumeric = (spec: FieldSpec) => spec.kind === 'number' || spec.kind === 'integer';

export function rangeHint(spec: FieldSpec): string {
  const pct = isPercent(spec);
  const show = (v: number) => String(pct ? clean(v * 100) : v).replace('.', ',') + (pct ? '%' : '');
  const { minimum: lo, maximum: hi } = spec;
  const range =
    lo != null && hi != null
      ? `de ${show(lo)} a ${show(hi)}`
      : hi != null
        ? `até ${show(hi)}`
        : lo != null
          ? `a partir de ${show(lo)}`
          : '';
  return range ? `Faixa: ${range}${spec.hard_limit ? ' (teto fixo de segurança)' : ''}.` : '';
}

export const defaultHint = (spec: FieldSpec): string => {
  const d = spec.default;
  if (d === undefined || d === null) return '';
  if (typeof d === 'boolean') return `Padrão recomendado: ${d ? 'ligado' : 'desligado'}.`;
  if (Array.isArray(d)) return `Padrão recomendado: ${d.length ? d.join(', ') : 'nenhum'}.`;
  if (isNumeric(spec))
    return `Padrão recomendado: ${toInput(spec, d).replace('.', ',')}${isPercent(spec) ? '%' : ''}.`;
  return `Padrão recomendado: ${String(d)}.`;
};

export const parseList = (text: string): string[] => [
  ...new Set(
    text
      .split(/[\s,;]+/)
      .map((s) => s.trim().toUpperCase())
      .filter(Boolean),
  ),
];

const stable = (v: unknown): string =>
  JSON.stringify(v, (_k, val: unknown) =>
    val !== null && typeof val === 'object' && !Array.isArray(val)
      ? Object.fromEntries(Object.entries(val).sort(([a], [b]) => a.localeCompare(b)))
      : val,
  );

export const sameValue = (a: unknown, b: unknown) => stable(a) === stable(b);
