import { XCircle } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';
import { ApiError } from '@/api/http';
import { onboardingApi, useSettings } from '@/api/queries';
import { ReauthCancelled, useFreshAuth } from '@/auth/ReauthContext';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/input';
import { RadioCard, RadioGroup } from '@/components/ui/radio-group';
import { Skeleton } from '@/components/ui/skeleton';
import { formatNumber } from '@/lib/format';

const RISK_OPTIONS = [
  { value: '0.25', title: '0,25%', description: 'Mais cauteloso: perdas menores.' },
  { value: '0.5', title: '0,5% (recomendado)', description: 'Equilíbrio entre risco e retorno.' },
  { value: '1', title: '1%', description: 'O máximo permitido por operação.' },
] as const;

const MAX_DAILY = 5;
const toDecimal = (pct: number) => (pct / 100).toFixed(4);

function failure(error: unknown): string {
  if (error instanceof ReauthCancelled) return 'Confirmação cancelada. Nada foi salvo.';
  if (error instanceof ApiError) {
    if (error.isNetwork) return 'Sem conexão com o servidor. Nada foi salvo.';
    if (error.status === 409)
      return 'A configuração mudou em outro lugar. Recarregue a página e tente de novo.';
    const field = error.problem.errors?.[0]?.message;
    if (field) return field;
    if (error.problem.detail) return error.problem.detail;
  }
  return 'Não foi possível salvar o risco agora. Tente de novo.';
}

/** Padroes recomendados ja marcados: quem so quer comecar clica em Continuar. */
export function RiskStep({ onBack, onNext }: { onBack: () => void; onNext: () => void }) {
  const uid = useId();
  const runFresh = useFreshAuth();
  const settings = useSettings();
  const [perTrade, setPerTrade] = useState<string>('0.5');
  const [daily, setDaily] = useState('1');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const capital = settings.data?.capital_reference ?? 10_000;
  const perTradeNum = Number(perTrade);
  const dailyNum = Number(daily.replace(',', '.'));
  const dailyError =
    !Number.isFinite(dailyNum) || dailyNum <= 0
      ? 'Digite um número maior que zero.'
      : dailyNum > MAX_DAILY
        ? `O máximo permitido é ${MAX_DAILY}%.`
        : dailyNum < perTradeNum
          ? 'A perda do dia não pode ser menor que a perda por operação.'
          : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (dailyError || !settings.data) return;
    setPending(true);
    setError(null);
    const value = { risk_per_trade: toDecimal(perTradeNum), max_daily_loss: toDecimal(dailyNum) };
    try {
      const check = await onboardingApi.validateRisk(value);
      if (!check.valid) {
        setError(check.errors[0]?.message ?? 'Algum valor não é aceito.');
        return;
      }
      await runFresh(() =>
        onboardingApi.saveRisk({
          value,
          reason: 'Configuração inicial do risco',
          expected_version: settings.data.version?.id ?? 0,
        }),
      );
      onNext();
    } catch (cause) {
      // Padrao recomendado ja salvo: a API recusa "nada a salvar" com 409, e aqui isso e sucesso.
      if (
        cause instanceof ApiError &&
        cause.status === 409 &&
        cause.problem.detail?.startsWith('Nada para salvar')
      ) {
        onNext();
        return;
      }
      setError(failure(cause));
    } finally {
      setPending(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-4" noValidate>
      <h1 className="text-[20px] font-bold tracking-[-0.01em]">Configurar o risco</h1>
      {settings.isPending ? (
        <div aria-busy="true" className="flex flex-col gap-3">
          <Skeleton className="h-11" />
          <Skeleton className="h-11" />
          <Skeleton className="h-11" />
        </div>
      ) : settings.isError ? (
        <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
          <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          Não foi possível carregar as configurações.{' '}
          <button type="button" className="underline" onClick={() => void settings.refetch()}>
            Tentar de novo
          </button>
        </p>
      ) : (
        <>
          <fieldset className="flex flex-col gap-2.5">
            <legend className="mb-1 font-semibold">
              Quanto você aceita perder em UMA operação?
            </legend>
            <RadioGroup
              value={perTrade}
              onValueChange={setPerTrade}
              aria-label="Perda máxima por operação"
              className="flex flex-col gap-2.5"
            >
              {RISK_OPTIONS.map((o) => (
                <RadioCard
                  key={o.value}
                  id={`${uid}-${o.value}`}
                  value={o.value}
                  title={o.title}
                  description={o.description}
                />
              ))}
            </RadioGroup>
            <p className="text-sec text-muted">
              Com {formatNumber(capital, 0)} USDT, cada perda custa no máximo{' '}
              <strong className="text-text">
                {formatNumber((capital * perTradeNum) / 100, 2)} USDT
              </strong>
              .
            </p>
          </fieldset>

          <Field
            label="Quanto você aceita perder em UM DIA? (%)"
            htmlFor={`${uid}-daily`}
            help={`Máximo ${MAX_DAILY}%. Ao chegar nele, o robô para de abrir operações até a meia-noite (UTC).`}
            helpId={`${uid}-daily-h`}
            error={dailyError}
            errorId={`${uid}-daily-e`}
          >
            <Input
              id={`${uid}-daily`}
              inputMode="decimal"
              value={daily}
              onChange={(e) => setDaily(e.target.value)}
              aria-invalid={dailyError ? true : undefined}
              aria-describedby={dailyError ? `${uid}-daily-e` : `${uid}-daily-h`}
              className="max-w-32"
            />
          </Field>

          <p className="text-sec">
            <strong>Em que queda o robô se desliga sozinho?</strong> 5% desde o ponto mais alto do
            saldo. Este limite é fixo e protege você.
          </p>
          <p
            role="status"
            className="rounded-xl border border-border-strong bg-surface-2 px-3.5 py-3"
          >
            <strong>Resumo:</strong> o robô arrisca no máximo{' '}
            {formatNumber(perTradeNum, perTradeNum < 1 ? 2 : 0)}% por operação, para o dia se perder{' '}
            {Number.isFinite(dailyNum) ? formatNumber(dailyNum, dailyNum % 1 ? 1 : 0) : '—'}% e se
            desliga se cair 5% desde o pico.
          </p>
        </>
      )}
      {error ? (
        <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
          <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap justify-between gap-2.5">
        <Button onClick={onBack}>Voltar</Button>
        <Button
          type="submit"
          variant="primary"
          disabled={pending || !settings.data || dailyError !== null}
        >
          {pending ? 'Salvando…' : 'Continuar'}
        </Button>
      </div>
    </form>
  );
}
