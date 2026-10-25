import { useId, useState, type ReactNode } from 'react';
import type { FieldSpec, FullSettings } from '@/api/settingsSchemas';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Field, Input } from '@/components/ui/input';
import { AiUsagePanel } from './AiUsagePanel';
import { relPath } from './fieldModel';
import { FieldControl } from './FieldControl';
import { SectionShell } from './SectionShell';
import { useSectionEditor, type EditorOptions, type SectionEditor } from './useSectionEditor';

type TabProps = Omit<EditorOptions, 'section'>;

const specIn = (editor: SectionEditor, rel: string): FieldSpec | undefined =>
  editor.specs.find((s) => relPath(editor.section, s.path) === rel);

function Fields({ editor, rels }: { editor: SectionEditor; rels: string[] }) {
  return (
    <>
      {rels.map((rel) => {
        const spec = specIn(editor, rel);
        return spec ? <FieldControl key={rel} editor={editor} spec={spec} /> : null;
      })}
    </>
  );
}

function Group({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <fieldset className="flex flex-col gap-3 rounded-xl border border-border p-3.5">
      <legend className="px-1.5 font-semibold">{title}</legend>
      {hint ? <p className="text-sec text-muted">{hint}</p> : null}
      <div className="grid gap-3.5 md:grid-cols-2">{children}</div>
    </fieldset>
  );
}

const SUGGESTED_PAIRS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT'];

export function PairsTab(props: TabProps) {
  const uid = useId();
  const editor = useSectionEditor({ section: 'pairs', ...props });
  const [extra, setExtra] = useState('');
  const symbolsSpec = specIn(editor, 'symbols');
  const timeframe = specIn(editor, 'timeframe');
  const raw = symbolsSpec ? editor.valueOf(symbolsSpec) : [];
  const chosen = Array.isArray(raw) ? (raw as string[]) : [];
  const options = [...new Set([...SUGGESTED_PAIRS, ...chosen])];
  const error = editor.fieldErrors.symbols;

  const set = (next: string[]) => symbolsSpec && editor.setValue(symbolsSpec, next);
  const add = () => {
    const v = extra.trim().toUpperCase();
    if (v && !chosen.includes(v)) set([...chosen, v]);
    setExtra('');
  };

  return (
    <SectionShell editor={editor}>
      <fieldset className="flex flex-col gap-2.5">
        <legend className="mb-1 font-semibold">Pares que o robô acompanha</legend>
        <div className="grid gap-2.5 sm:grid-cols-2">
          {options.map((p) => (
            <label
              key={p}
              htmlFor={`${uid}-${p}`}
              className="flex min-h-11 cursor-pointer items-center gap-2.5 rounded-xl border border-border-strong px-3 py-2.5 has-[[data-state=checked]]:border-info has-[[data-state=checked]]:bg-info-bg"
            >
              <Checkbox
                id={`${uid}-${p}`}
                checked={chosen.includes(p)}
                disabled={!editor.canEdit}
                onCheckedChange={(on) =>
                  set(on === true ? [...chosen, p] : chosen.filter((c) => c !== p))
                }
              />
              <strong>{p}</strong>
            </label>
          ))}
        </div>
        {error ? (
          <p role="alert" className="text-cap text-loss">
            {error}
          </p>
        ) : null}
        <p className="text-sec text-muted">
          Pausar um par com posição aberta não desprotege a posição: o stop continua valendo. Ao
          incluir um par novo, o robô primeiro carrega o histórico (pelo menos 20 velas).
        </p>
      </fieldset>
      <div className="flex items-end gap-2.5">
        <Field label="Incluir outro par" htmlFor={`${uid}-extra`} help="Exemplo: ADA/USDT">
          <Input
            id={`${uid}-extra`}
            value={extra}
            autoComplete="off"
            disabled={!editor.canEdit}
            onChange={(e) => setExtra(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                add();
              }
            }}
          />
        </Field>
        <Button disabled={!editor.canEdit || !extra.trim()} onClick={add}>
          Incluir
        </Button>
      </div>
      {timeframe ? <FieldControl editor={editor} spec={timeframe} /> : null}
    </SectionShell>
  );
}

export function StrategiesTab(props: TabProps) {
  const editor = useSectionEditor({ section: 'strategies', ...props });
  const detector = editor.specs.filter((s) => s.path.startsWith('strategies.elephant.'));
  const ema = editor.specs.filter((s) => s.path.startsWith('strategies.ema.'));
  return (
    <SectionShell editor={editor}>
      <Group title="Estratégias ativas" hint="Ao menos uma precisa ficar ligada.">
        <Fields editor={editor} rels={['elephant_enabled', 'ema_enabled']} />
      </Group>
      <Group
        title="Sinais"
        hint="Mudar estes valores muda quantas entradas o robô faz. Teste no Backtest antes de salvar."
      >
        <Fields editor={editor} rels={['min_confidence', 'reward_risk']} />
      </Group>
      <details className="rounded-xl border border-border p-3.5">
        <summary className="min-h-11 cursor-pointer py-2.5 font-semibold">
          Detector da barra elefante e filtros
        </summary>
        <p className="text-sec text-muted">
          Sobre o conceito: <GlossaryTerm term="barra-elefante" />.
        </p>
        <div className="mt-3 grid gap-3.5 md:grid-cols-2">
          {detector.map((s) => (
            <FieldControl key={s.path} editor={editor} spec={s} />
          ))}
        </div>
      </details>
      <details className="rounded-xl border border-border p-3.5">
        <summary className="min-h-11 cursor-pointer py-2.5 font-semibold">
          Cruzamento de médias (EMA)
        </summary>
        <div className="mt-3 grid gap-3.5 md:grid-cols-2">
          {ema.map((s) => (
            <FieldControl key={s.path} editor={editor} spec={s} />
          ))}
        </div>
      </details>
    </SectionShell>
  );
}

const RISK_GROUPS: [string, string, string[]][] = [
  [
    'Por operação',
    'Quanto cada operação pode custar.',
    ['risk_per_trade', 'min_reward_risk', 'max_position_fraction', 'max_open_positions'],
  ],
  [
    'Limites do dia e do saldo',
    'Quando o robô para sozinho.',
    [
      'max_daily_loss',
      'daily_profit_target',
      'max_drawdown',
      'consecutive_loss_limit',
      'pair_pause_hours',
    ],
  ],
  [
    'Exposição',
    'Quanto do saldo pode ficar em moedas.',
    ['max_total_exposure_fraction', 'max_coin_exposure_fraction'],
  ],
  [
    'Filtros de mercado',
    'Condições em que o robô prefere não entrar.',
    [
      'max_spread',
      'min_quote_volume_24h',
      'event_margin_before_minutes',
      'event_margin_after_minutes',
      'max_signal_age_timeframes',
      'max_entry_deviation',
      'max_quote_age_seconds',
      'min_history_candles',
      'max_entry_fee_multiple',
    ],
  ],
  [
    'Saídas e taxas',
    '',
    ['partial_exit_fraction', 'trailing_r', 'profit_giveback', 'taker_fee_rate'],
  ],
  [
    'Moedas',
    'Separe por vírgula. A moeda de cotação e a da taxa não mudam aqui.',
    ['allowed_assets', 'blocked_assets'],
  ],
];
const HIDDEN_RISK = new Set(['quote_asset', 'fee_asset']);

export function RiskTab({ settings, ...props }: TabProps & { settings: FullSettings }) {
  const editor = useSectionEditor({ section: 'risk', settings, ...props });
  const listed = new Set([...RISK_GROUPS.flatMap(([, , r]) => r), ...HIDDEN_RISK]);
  const rest = editor.specs.map((s) => relPath('risk', s.path)).filter((rel) => !listed.has(rel));
  return (
    <SectionShell editor={editor}>
      <section
        aria-label="Tetos de segurança"
        className="rounded-xl border border-border-strong bg-surface-2 px-4 py-3"
      >
        <strong>Tetos fixos de segurança</strong>
        <p className="text-sec text-muted">O servidor recusa qualquer valor fora destes limites.</p>
        <ul className="mt-1.5 grid list-disc gap-x-6 pl-5 sm:grid-cols-2">
          {settings.hard_limits.map((h) => (
            <li key={h.path}>{h.text}</li>
          ))}
        </ul>
      </section>
      {RISK_GROUPS.map(([title, hint, rels]) => (
        <Group key={title} title={title} hint={hint || undefined}>
          <Fields editor={editor} rels={rels} />
        </Group>
      ))}
      {rest.length > 0 ? (
        <Group title="Outros">
          <Fields editor={editor} rels={rest} />
        </Group>
      ) : null}
    </SectionShell>
  );
}

export function AiTab(props: TabProps) {
  const editor = useSectionEditor({ section: 'ai', ...props });
  return (
    <SectionShell editor={editor}>
      <AiUsagePanel />
      <Fields editor={editor} rels={['enabled', 'model']} />
    </SectionShell>
  );
}
