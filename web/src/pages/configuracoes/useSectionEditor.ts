import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { ApiError } from '@/api/http';
import { settingsApi } from '@/api/settingsQueries';
import type {
  FieldSpec,
  FullSettings,
  SectionName,
  SectionValue,
  UpdateResult,
} from '@/api/settingsSchemas';
import { ReauthCancelled, useFreshAuth } from '@/auth/ReauthContext';
import { fromInput, getAt, isNumeric, relPath, sameValue, setAt, toInput } from './fieldModel';

const DEFAULT_REASON: Record<SectionName, string> = {
  pairs: 'Ajuste dos pares',
  strategies: 'Ajuste das estratégias',
  risk: 'Ajuste do risco',
  ai: 'Ajuste da IA',
};

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return debounced;
}

export interface SaveFailure {
  conflict: boolean;
  text: string;
}

export function saveFailure(error: unknown): SaveFailure {
  if (error instanceof ReauthCancelled)
    return { conflict: false, text: 'Confirmação cancelada. Nada foi salvo.' };
  if (error instanceof ApiError) {
    if (error.isNetwork)
      return { conflict: false, text: 'Sem conexão com o servidor. Nada foi salvo.' };
    if (error.status === 409 && (error.problem.detail ?? '').includes('mudou enquanto'))
      return { conflict: true, text: error.problem.detail ?? '' };
    if (error.status === 403 && !error.needsFreshAuth)
      return { conflict: false, text: 'Só administradores alteram as configurações.' };
    const first = error.problem.errors?.[0]?.message ?? error.problem.detail;
    if (first) return { conflict: false, text: first };
  }
  return { conflict: false, text: 'Não foi possível salvar agora. Nada mudou. Tente de novo.' };
}

export interface EditorOptions {
  section: SectionName;
  settings: FullSettings;
  onSaved: (result: UpdateResult) => void;
  onConflict: (text: string) => void;
}

/**
 * Rascunho de uma secao: numeros ficam como texto enquanto a pessoa digita; o valor completo da secao e
 * montado a cada render e validado pelo servidor (com atraso) para mostrar erros e aumento de risco.
 */
export function useSectionEditor({ section, settings, onSaved, onConflict }: EditorOptions) {
  const runFresh = useFreshAuth();
  const specs: FieldSpec[] = settings.fields[section];
  const base: SectionValue = settings.document[section];
  const canEdit = settings.can_edit;
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [overrides, setOverrides] = useState<Record<string, unknown>>({});
  const [reason, setReason] = useState(DEFAULT_REASON[section]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const { value, localErrors } = useMemo(() => {
    let next = base;
    const errors: Record<string, string> = {};
    for (const [rel, v] of Object.entries(overrides)) next = setAt(next, rel, v);
    for (const spec of specs) {
      const rel = relPath(section, spec.path);
      const text = inputs[rel];
      if (!isNumeric(spec) || text === undefined) continue;
      const parsed = fromInput(spec, text, getAt(base, rel));
      if (parsed === undefined) errors[rel] = 'Digite um número.';
      else next = setAt(next, rel, parsed);
    }
    return { value: next, localErrors: errors };
  }, [base, inputs, overrides, section, specs]);

  const dirty = !sameValue(value, base);
  const debounced = useDebounced(value, 400);
  const current = sameValue(debounced, value);
  const hasLocalErrors = Object.keys(localErrors).length > 0;
  const check = useQuery({
    queryKey: [
      'settings',
      'validate',
      section,
      JSON.stringify(debounced),
      settings.version?.id ?? 0,
    ],
    queryFn: ({ signal }) => settingsApi.validate(section, debounced, signal),
    enabled: canEdit && dirty && current && !hasLocalErrors,
    staleTime: Infinity,
    retry: false,
  });
  const validation = current && dirty && !hasLocalErrors ? (check.data ?? null) : null;
  const checking = dirty && !hasLocalErrors && (!current || check.isFetching);

  const fieldErrors: Record<string, string> = { ...localErrors };
  for (const e of validation?.errors ?? []) fieldErrors[relPath(section, e.path)] = e.message;

  const valid = dirty && !hasLocalErrors && validation !== null && validation.valid && !checking;
  const needsAck = validation?.requires_acknowledgement === true;

  const performSave = async (acknowledge: boolean) => {
    setSaving(true);
    setError(null);
    try {
      const out = await runFresh(() =>
        settingsApi.save(section, {
          value,
          reason: reason.trim(),
          expected_version: settings.version?.id ?? 0,
          acknowledge_risk_increase: acknowledge,
        }),
      );
      onSaved(out);
    } catch (cause) {
      const failure = saveFailure(cause);
      if (failure.conflict) return onConflict(failure.text);
      throw new Error(failure.text, { cause });
    } finally {
      setSaving(false);
    }
  };

  const save = async () => {
    if (needsAck) return setConfirmOpen(true);

    try {
      await performSave(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Não foi possível salvar agora.');
    }
  };

  return {
    section,
    specs,
    canEdit,
    value,
    dirty,
    valid,
    checking,
    validation,
    fieldErrors,
    reason,
    setReason,
    saving,
    error,
    needsAck,
    confirmOpen,
    setConfirmOpen,
    performSave,
    save,
    textOf: (spec: FieldSpec) => {
      const rel = relPath(section, spec.path);
      const typed = inputs[rel];
      if (typed !== undefined) return typed;
      const current = rel in overrides ? overrides[rel] : getAt(base, rel);
      if (spec.kind === 'list') return Array.isArray(current) ? current.join(', ') : '';
      return toInput(spec, current);
    },
    valueOf: (spec: FieldSpec): unknown => {
      const rel = relPath(section, spec.path);
      return rel in overrides ? overrides[rel] : getAt(base, rel);
    },
    setText: (spec: FieldSpec, text: string) =>
      setInputs((cur) => ({ ...cur, [relPath(section, spec.path)]: text })),
    setValue: (spec: FieldSpec, v: unknown) =>
      setOverrides((cur) => ({ ...cur, [relPath(section, spec.path)]: v })),
    discard: () => {
      setInputs({});
      setOverrides({});
      setError(null);
    },
    applyDefaults: () => {
      const defaults = settings.defaults[section];
      const nextInputs: Record<string, string> = {};
      const nextOverrides: Record<string, unknown> = {};
      for (const spec of specs) {
        const rel = relPath(section, spec.path);
        const d = getAt(defaults, rel);
        if (isNumeric(spec)) nextInputs[rel] = toInput(spec, d);
        else nextOverrides[rel] = d;
      }
      setInputs(nextInputs);
      setOverrides(nextOverrides);
    },
  };
}

export type SectionEditor = ReturnType<typeof useSectionEditor>;
