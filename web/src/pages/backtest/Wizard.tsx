import { useQueryClient } from '@tanstack/react-query';
import { ChevronLeft, ChevronRight, Play, TriangleAlert, XCircle } from 'lucide-react';
import { useId, useState, type ReactNode } from 'react';
import { backtestApi, BACKTEST_KEYS } from '@/api/backtestQueries';
import { ApiError } from '@/api/http';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Field, Input } from '@/components/ui/input';
import { RadioCard, RadioGroup } from '@/components/ui/radio-group';
import { Select } from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { cn } from '@/lib/utils';
import {
  buildRequest,
  DEFAULT_WIZARD,
  describePeriod,
  firstInvalidStep,
  MAX_SYMBOLS,
  MIN_START,
  PAIR_OPTIONS,
  PRESETS,
  STEPS,
  STRATEGY_OPTIONS,
  TIMEFRAMES,
  todayIso,
  toNumber,
  validateAll,
  validateStep,
  type Errors,
  type WizardState,
} from './wizardModel';

function CheckRow({
  id,
  checked,
  onChange,
  title,
  hint,
  disabled,
}: {
  id: string;
  checked: boolean;
  onChange: (next: boolean) => void;
  title: ReactNode;
  hint?: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label
      htmlFor={id}
      className={cn(
        'flex min-h-11 cursor-pointer items-start gap-2.5 rounded-xl border border-border-strong px-3 py-2.5 has-[[data-state=checked]]:border-info has-[[data-state=checked]]:bg-info-bg',
        disabled && 'cursor-not-allowed opacity-70',
      )}
    >
      <Checkbox
        id={id}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(v) => onChange(v === true)}
        className="mt-0.5"
      />
      <span className="flex flex-col">
        <strong>{title}</strong>
        {hint ? <span className="text-sec font-normal text-muted">{hint}</span> : null}
      </span>
    </label>
  );
}

const fail = (error: unknown): string => {
  if (error instanceof ApiError) {
    if (error.isNetwork) return 'Sem conexão com o servidor. O teste não foi criado.';
    const first = error.problem.errors?.[0]?.message;
    return first ?? error.problem.detail ?? 'O servidor não aceitou este teste.';
  }
  return 'Não foi possível criar o teste agora. Tente de novo.';
};

export function Wizard({
  canRun,
  onCreated,
  initial = DEFAULT_WIZARD,
}: {
  canRun: boolean;
  onCreated: (jobId: string) => void;
  initial?: WizardState;
}) {
  const uid = useId();
  const qc = useQueryClient();
  const [step, setStep] = useState(0);
  const [s, setS] = useState<WizardState>(initial);
  const [shown, setShown] = useState<Errors>({});
  const [extraPair, setExtraPair] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof WizardState>(key: K, value: WizardState[K]) => {
    setS((cur) => ({ ...cur, [key]: value }));
    setShown((cur) => ({ ...cur, [key]: undefined }));
  };
  const live = validateStep(step, s);
  const today = todayIso();

  const toggle = (key: 'symbols' | 'strategies', value: string, on: boolean) =>
    set(key, on ? [...s[key], value] : s[key].filter((v) => v !== value));

  const next = () => {
    setShown(live);
    if (Object.keys(live).length === 0) setStep((n) => Math.min(n + 1, STEPS.length - 1));
  };

  const run = async () => {
    const wrong = firstInvalidStep(s);
    if (wrong >= 0) {
      setShown(validateAll(s));
      setStep(wrong);
      return;
    }
    setPending(true);
    setError(null);
    try {
      const job = await backtestApi.create(buildRequest(s));
      await qc.invalidateQueries({ queryKey: BACKTEST_KEYS.list });
      onCreated(job.id);
    } catch (cause) {
      setError(fail(cause));
    } finally {
      setPending(false);
    }
  };

  const addPair = () => {
    const value = extraPair.trim().toUpperCase();
    if (!value || s.symbols.includes(value)) return setExtraPair('');
    if (s.symbols.length >= MAX_SYMBOLS) return set('symbols', s.symbols);
    set('symbols', [...s.symbols, value]);
    setExtraPair('');
  };

  const err = (key: keyof Errors) => shown[key];
  const fieldProps = (key: keyof WizardState) => ({
    'aria-invalid': err(key) ? (true as const) : undefined,
    'aria-describedby': err(key) ? `${uid}-${key}-e` : undefined,
  });
  const field = (key: keyof WizardState, label: ReactNode, help?: ReactNode) => ({
    label,
    htmlFor: `${uid}-${key}`,
    help,
    helpId: `${uid}-${key}-h`,
    error: err(key),
    errorId: `${uid}-${key}-e`,
  });

  const wf = s.walkForward;
  const customPairs = s.symbols.filter((p) => !(PAIR_OPTIONS as readonly string[]).includes(p));

  return (
    <Card aria-labelledby="wiz-t">
      <CardHeader>
        <CardTitle id="wiz-t">
          <Play className="size-5" aria-hidden="true" /> Novo backtest
        </CardTitle>
        <ModeBadge mode="backtest" />
      </CardHeader>
      <div className="flex flex-col gap-5 lg:flex-row">
        <nav aria-label="Etapas do backtest" className="lg:w-48 lg:shrink-0">
          <ol className="flex gap-1.5 overflow-x-auto lg:flex-col">
            {STEPS.map((label, i) => (
              <li key={label} className="shrink-0">
                <button
                  type="button"
                  disabled={i > step}
                  aria-current={i === step ? 'step' : undefined}
                  onClick={() => setStep(i)}
                  className={cn(
                    'flex min-h-11 w-full cursor-pointer items-center gap-2 rounded-control border border-transparent bg-transparent px-2.5 text-left font-semibold text-muted disabled:cursor-default',
                    i === step && 'border-backtest bg-backtest-bg text-text',
                    i < step && 'text-text',
                  )}
                >
                  <span
                    aria-hidden="true"
                    className="grid size-6 shrink-0 place-items-center rounded-full border border-border-strong text-cap"
                  >
                    {i < step ? '✓' : i + 1}
                  </span>
                  <span>
                    {label}
                    {i < step ? <span className="sr-only"> (concluída)</span> : null}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </nav>

        <form
          className="flex min-w-0 flex-1 flex-col gap-4"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            if (step === STEPS.length - 1) void run();
            else next();
          }}
        >
          <h3 className="text-[18px] font-bold">
            Etapa {step + 1} de {STEPS.length}: {STEPS[step]}
          </h3>

          {step === 0 ? (
            <>
              <fieldset className="flex flex-col gap-2.5">
                <legend className="mb-1 font-semibold">
                  Quais pares você quer testar? (até {MAX_SYMBOLS})
                </legend>
                <div className="grid gap-2.5 sm:grid-cols-2">
                  {[...PAIR_OPTIONS, ...customPairs].map((p) => (
                    <CheckRow
                      key={p}
                      id={`${uid}-p-${p}`}
                      title={p}
                      checked={s.symbols.includes(p)}
                      onChange={(on) => toggle('symbols', p, on)}
                    />
                  ))}
                </div>
                {err('symbols') ? (
                  <p role="alert" className="flex items-center gap-1.5 text-cap text-loss">
                    <XCircle className="size-4" aria-hidden="true" />
                    {err('symbols')}
                  </p>
                ) : null}
              </fieldset>
              <div className="flex items-end gap-2.5">
                <Field label="Outro par" htmlFor={`${uid}-extra`} help="Exemplo: ADA/USDT">
                  <Input
                    id={`${uid}-extra`}
                    value={extraPair}
                    autoComplete="off"
                    onChange={(e) => setExtraPair(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        addPair();
                      }
                    }}
                  />
                </Field>
                <Button onClick={addPair} disabled={!extraPair.trim()}>
                  Adicionar
                </Button>
              </div>
              <Field label="Tempo de cada vela" htmlFor={`${uid}-timeframe`}>
                <Select
                  id={`${uid}-timeframe`}
                  value={s.timeframe}
                  onChange={(e) => set('timeframe', e.target.value)}
                >
                  {TIMEFRAMES.map(([v, l]) => (
                    <option key={v} value={v}>
                      {l}
                    </option>
                  ))}
                </Select>
              </Field>
            </>
          ) : null}

          {step === 1 ? (
            <>
              <RadioGroup
                value={s.preset}
                onValueChange={(v) => set('preset', v)}
                aria-label="Período do teste"
                className="flex flex-col gap-2.5"
              >
                {PRESETS.map(([v, l]) => (
                  <RadioCard key={v} id={`${uid}-r-${v}`} value={v} title={l} />
                ))}
              </RadioGroup>
              {s.preset === 'custom' ? (
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field
                    {...field(
                      'start',
                      'Data inicial',
                      'Dados da Binance existem desde 17/08/2017.',
                    )}
                  >
                    <Input
                      id={`${uid}-start`}
                      type="date"
                      min={MIN_START}
                      max={today}
                      value={s.start}
                      onChange={(e) => set('start', e.target.value)}
                      {...fieldProps('start')}
                    />
                  </Field>
                  <Field {...field('end', 'Data final')}>
                    <Input
                      id={`${uid}-end`}
                      type="date"
                      min={MIN_START}
                      max={today}
                      value={s.end}
                      onChange={(e) => set('end', e.target.value)}
                      {...fieldProps('end')}
                    />
                  </Field>
                </div>
              ) : null}
              {err('period') ? (
                <p role="alert" className="text-sec text-loss">
                  {err('period')}
                </p>
              ) : null}
              <p className="text-sec text-muted">
                Períodos longos levam mais tempo para baixar e simular. Você acompanha o progresso e
                pode cancelar.
              </p>
            </>
          ) : null}

          {step === 2 ? (
            <fieldset className="flex flex-col gap-2.5">
              <legend className="mb-1 font-semibold">Quais estratégias entram no teste?</legend>
              {STRATEGY_OPTIONS.map(([v, title, hint]) => (
                <CheckRow
                  key={v}
                  id={`${uid}-s-${v}`}
                  title={title}
                  hint={hint}
                  checked={s.strategies.includes(v)}
                  onChange={(on) => toggle('strategies', v, on)}
                />
              ))}
              {err('strategies') ? (
                <p role="alert" className="flex items-center gap-1.5 text-cap text-loss">
                  <XCircle className="size-4" aria-hidden="true" />
                  {err('strategies')}
                </p>
              ) : null}
              <p className="text-sec text-muted">
                A <GlossaryTerm term="barra-elefante" /> e o cruzamento de médias usam os padrões do
                robô. Ajuste só se quiser comparar.
              </p>
            </fieldset>
          ) : null}

          {step === 3 ? (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field {...field('capital', 'Capital inicial (USDT)')}>
                  <Input
                    id={`${uid}-capital`}
                    inputMode="decimal"
                    value={s.capital}
                    onChange={(e) => set('capital', e.target.value)}
                    {...fieldProps('capital')}
                  />
                </Field>
                <Field
                  {...field(
                    'takerFeePct',
                    'Taxa ao entrar a mercado (%)',
                    'Padrão da Binance: 0,10%.',
                  )}
                >
                  <Input
                    id={`${uid}-takerFeePct`}
                    inputMode="decimal"
                    value={s.takerFeePct}
                    onChange={(e) => set('takerFeePct', e.target.value)}
                    {...fieldProps('takerFeePct')}
                  />
                </Field>
                <Field {...field('makerFeePct', 'Taxa em ordens limitadas (%)')}>
                  <Input
                    id={`${uid}-makerFeePct`}
                    inputMode="decimal"
                    value={s.makerFeePct}
                    onChange={(e) => set('makerFeePct', e.target.value)}
                    {...fieldProps('makerFeePct')}
                  />
                </Field>
                <Field
                  {...field(
                    'slippageBps',
                    <>
                      <GlossaryTerm term="deslizamento" /> (pontos-base)
                    </>,
                    '5 pontos-base são 0,05%.',
                  )}
                >
                  <Input
                    id={`${uid}-slippageBps`}
                    inputMode="decimal"
                    value={s.slippageBps}
                    onChange={(e) => set('slippageBps', e.target.value)}
                    {...fieldProps('slippageBps')}
                  />
                </Field>
                <Field
                  {...field(
                    'latency',
                    <>
                      <GlossaryTerm term="latencia" /> (velas)
                    </>,
                    'De 1 a 10 velas.',
                  )}
                >
                  <Input
                    id={`${uid}-latency`}
                    inputMode="numeric"
                    value={s.latency}
                    onChange={(e) => set('latency', e.target.value)}
                    {...fieldProps('latency')}
                  />
                </Field>
              </div>

              <fieldset className="flex flex-col gap-3 rounded-xl border border-border p-3.5">
                <legend className="px-1.5 font-semibold">Parâmetros (opcional)</legend>
                <p className="text-sec text-muted">
                  Deixe em branco para usar o padrão de cada estratégia.
                </p>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Field
                    {...field(
                      'minConfidence',
                      <GlossaryTerm term="confianca-minima" />,
                      'De 0 a 1.',
                    )}
                  >
                    <Input
                      id={`${uid}-minConfidence`}
                      inputMode="decimal"
                      placeholder="padrão"
                      value={s.minConfidence}
                      onChange={(e) => set('minConfidence', e.target.value)}
                      {...fieldProps('minConfidence')}
                    />
                  </Field>
                  <Field
                    {...field(
                      'rsiMin',
                      <GlossaryTerm term="rsi">RSI mínimo</GlossaryTerm>,
                      'De 0 a 99.',
                    )}
                  >
                    <Input
                      id={`${uid}-rsiMin`}
                      inputMode="decimal"
                      placeholder="padrão"
                      value={s.rsiMin}
                      onChange={(e) => set('rsiMin', e.target.value)}
                      {...fieldProps('rsiMin')}
                    />
                  </Field>
                  <Field
                    {...field('rewardRisk', <GlossaryTerm term="retorno-risco" />, 'De 2 a 10.')}
                  >
                    <Input
                      id={`${uid}-rewardRisk`}
                      inputMode="decimal"
                      placeholder="padrão"
                      value={s.rewardRisk}
                      onChange={(e) => set('rewardRisk', e.target.value)}
                      {...fieldProps('rewardRisk')}
                    />
                  </Field>
                </div>
              </fieldset>

              <div className="flex flex-col gap-3 rounded-xl border border-border p-3.5">
                <div className="flex items-center justify-between gap-3">
                  <label htmlFor={`${uid}-wf`} className="font-semibold">
                    Teste em janelas (<GlossaryTerm term="walk-forward" />)
                    <span className="block text-sec font-normal text-muted">
                      Recomendado: confere o resultado fora da amostra.
                    </span>
                  </label>
                  <Switch
                    id={`${uid}-wf`}
                    checked={wf}
                    onCheckedChange={(v) => set('walkForward', v)}
                  />
                </div>
                {wf ? (
                  <>
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Field {...field('trainDays', 'Dias de treino', 'De 7 a 1500.')}>
                        <Input
                          id={`${uid}-trainDays`}
                          inputMode="numeric"
                          value={s.trainDays}
                          onChange={(e) => set('trainDays', e.target.value)}
                          {...fieldProps('trainDays')}
                        />
                      </Field>
                      <Field {...field('testDays', 'Dias de teste', 'De 1 a 720.')}>
                        <Input
                          id={`${uid}-testDays`}
                          inputMode="numeric"
                          value={s.testDays}
                          onChange={(e) => set('testDays', e.target.value)}
                          {...fieldProps('testDays')}
                        />
                      </Field>
                    </div>
                    <Field
                      {...field(
                        'grid',
                        'Grade de parâmetros (opcional)',
                        'Exemplo: min_confidence=0.5,0.6;rsi_min=45,50',
                      )}
                    >
                      <Input
                        id={`${uid}-grid`}
                        value={s.grid}
                        placeholder="padrão do robô"
                        onChange={(e) => set('grid', e.target.value)}
                        {...fieldProps('grid')}
                      />
                    </Field>
                    <p className="flex items-start gap-2 text-sec text-muted">
                      <TriangleAlert
                        className="mt-0.5 size-4 shrink-0 text-warn"
                        aria-hidden="true"
                      />
                      <span>
                        Quanto mais combinações, maior o risco de enganar-se (
                        <GlossaryTerm term="sobreajuste" />
                        ). O limite é de 24 combinações.
                      </span>
                    </p>
                  </>
                ) : null}
              </div>
              {err('period') ? (
                <p role="alert" className="text-sec text-loss">
                  {err('period')}
                </p>
              ) : null}
            </>
          ) : null}

          {step === 4 ? (
            <div className="flex flex-col gap-3">
              <ul className="flex flex-col gap-1.5 rounded-xl border border-border-strong bg-surface-2 px-4 py-3">
                <li>
                  Pares: <strong>{s.symbols.join(', ')}</strong>, velas de{' '}
                  {TIMEFRAMES.find(([v]) => v === s.timeframe)?.[1].replace(' (recomendado)', '')}.
                </li>
                <li>
                  Período: <strong>{describePeriod(s)}</strong>.
                </li>
                <li>
                  Estratégias:{' '}
                  <strong>
                    {STRATEGY_OPTIONS.filter(([v]) => s.strategies.includes(v))
                      .map(([, t]) => t)
                      .join(' e ')}
                  </strong>
                  .
                </li>
                <li>
                  Capital de {toNumber(s.capital).toLocaleString('pt-BR')} USDT, taxa de{' '}
                  {s.takerFeePct}%, deslizamento de {s.slippageBps} pontos-base e latência de{' '}
                  {s.latency} vela(s).
                </li>
                <li>
                  {wf
                    ? `Com teste em janelas: treino de ${s.trainDays} dias e teste de ${s.testDays} dias.`
                    : 'Sem teste em janelas (o resultado não será conferido fora da amostra).'}
                </li>
              </ul>
              <p className="text-sec text-muted">
                O teste roda no servidor: você pode sair da página e voltar depois. Resultado de
                simulação no passado não garante o futuro.
              </p>
              {canRun ? null : (
                <p
                  role="note"
                  className="rounded-xl border border-border-strong bg-surface-2 px-3.5 py-2.5 text-sec"
                >
                  Só administradores iniciam backtests. Você pode ver o histórico e os relatórios.
                </p>
              )}
              {error ? (
                <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
                  <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  {error}
                </p>
              ) : null}
            </div>
          ) : null}

          <div className="flex flex-wrap justify-between gap-2.5">
            <Button disabled={step === 0 || pending} onClick={() => setStep((n) => n - 1)}>
              <ChevronLeft className="size-4" aria-hidden="true" /> Voltar
            </Button>
            {step < STEPS.length - 1 ? (
              <Button type="submit" variant="primary">
                Continuar <ChevronRight className="size-4" aria-hidden="true" />
              </Button>
            ) : (
              <Button type="submit" variant="primary" disabled={!canRun || pending}>
                <Play className="size-4" aria-hidden="true" />
                {pending ? 'Criando…' : 'Rodar backtest'}
              </Button>
            )}
          </div>
        </form>
      </div>
    </Card>
  );
}
