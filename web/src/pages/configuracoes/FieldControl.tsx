import { useId, type ReactNode } from 'react';
import type { FieldSpec } from '@/api/settingsSchemas';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { Field, Input } from '@/components/ui/input';
import { Select } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import type { GlossaryKey } from '@/lib/glossary';
import { defaultHint, isNumeric, parseList, rangeHint, relPath } from './fieldModel';
import type { SectionEditor } from './useSectionEditor';

const TERM: Record<string, GlossaryKey> = {
  'risk.max_drawdown': 'drawdown',
  'risk.max_daily_loss': 'perda-diaria',
  'risk.max_total_exposure_fraction': 'exposicao',
  'risk.max_coin_exposure_fraction': 'exposicao',
  'risk.event_margin_before_minutes': 'janela-de-evento',
  'risk.event_margin_after_minutes': 'janela-de-evento',
  'risk.min_reward_risk': 'retorno-risco',
  'risk.trailing_r': 'r',
  'strategies.reward_risk': 'retorno-risco',
  'strategies.min_confidence': 'confianca-minima',
  'strategies.elephant.rsi_min': 'rsi',
  'strategies.elephant.rsi_max': 'rsi',
  'strategies.ema.rsi_min': 'rsi',
  'strategies.ema.rsi_max': 'rsi',
  'strategies.elephant_enabled': 'barra-elefante',
};

export function FieldControl({
  editor,
  spec,
  extra,
}: {
  editor: SectionEditor;
  spec: FieldSpec;
  extra?: ReactNode;
}) {
  const uid = useId();
  const id = `${uid}-f`;
  const rel = relPath(editor.section, spec.path);
  const error = editor.fieldErrors[rel];
  const disabled = !editor.canEdit;
  const term = TERM[spec.path];
  const label = term ? <GlossaryTerm term={term}>{spec.label}</GlossaryTerm> : spec.label;
  const help = [rangeHint(spec), defaultHint(spec)].filter(Boolean).join(' ');
  const errorProps = error
    ? { 'aria-invalid': true as const, 'aria-describedby': `${id}-e` }
    : { 'aria-describedby': `${id}-h` };

  if (spec.kind === 'boolean') {
    const on = editor.valueOf(spec) === true;
    return (
      <div className="flex items-center justify-between gap-3 rounded-xl border border-border px-3.5 py-3">
        <label htmlFor={id} className="flex flex-col font-semibold">
          {label}
          <span id={`${id}-h`} className="text-sec font-normal text-muted">
            {defaultHint(spec)}
          </span>
        </label>
        <Switch
          id={id}
          checked={on}
          disabled={disabled}
          aria-describedby={`${id}-h`}
          onCheckedChange={(v) => editor.setValue(spec, v)}
        />
      </div>
    );
  }

  const field = (control: ReactNode) => (
    <Field
      label={
        <>
          {label}
          {spec.unit === '%' ? ' (%)' : ''}
        </>
      }
      htmlFor={id}
      help={help}
      helpId={`${id}-h`}
      error={error}
      errorId={`${id}-e`}
    >
      {control}
      {extra}
    </Field>
  );

  if (spec.kind === 'choice')
    return field(
      <Select
        id={id}
        value={String(editor.valueOf(spec) ?? '')}
        disabled={disabled}
        onChange={(e) => editor.setValue(spec, e.target.value)}
        {...errorProps}
      >
        {(spec.choices ?? []).map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </Select>,
    );

  if (spec.kind === 'list')
    return field(
      <Input
        id={id}
        autoComplete="off"
        disabled={disabled}
        value={editor.textOf(spec)}
        onChange={(e) => {
          editor.setText(spec, e.target.value);
          editor.setValue(spec, parseList(e.target.value));
        }}
        {...errorProps}
      />,
    );

  if (isNumeric(spec))
    return field(
      <Input
        id={id}
        inputMode={spec.kind === 'integer' ? 'numeric' : 'decimal'}
        autoComplete="off"
        disabled={disabled}
        value={editor.textOf(spec)}
        onChange={(e) => editor.setText(spec, e.target.value)}
        className="max-w-48"
        {...errorProps}
      />,
    );

  return field(
    <Input
      id={id}
      autoComplete="off"
      disabled={disabled}
      value={String(editor.valueOf(spec) ?? '')}
      onChange={(e) => editor.setValue(spec, e.target.value)}
      {...errorProps}
    />,
  );
}
