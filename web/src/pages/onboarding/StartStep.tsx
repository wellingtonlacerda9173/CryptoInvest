import { TriangleAlert, XCircle } from 'lucide-react';
import { useState } from 'react';
import { ApiError } from '@/api/http';
import { onboardingApi, useSettings } from '@/api/queries';
import { useFreshAuth } from '@/auth/ReauthContext';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { notify } from '@/components/kit/Toaster';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { formatNumber } from '@/lib/format';

export function StartStep({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const runFresh = useFreshAuth();
  const settings = useSettings();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const doc = settings.data?.document;

  const start = async () => {
    setPending(true);
    setError(null);
    try {
      const out = await runFresh(() => onboardingApi.startPaper());
      if (out.state === 'waiting_runner') {
        notify.warn(
          'Configuração salva',
          'O robô ainda não está ligado. Ligue-o pela tela Controle quando quiser.',
        );
      } else {
        notify.success('Seu robô está rodando em PAPER', 'Dinheiro de mentira, preços reais.');
      }
      onDone();
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.isNetwork
          ? 'Sem conexão com o servidor. Nada foi iniciado.'
          : cause instanceof ApiError && cause.problem.detail
            ? cause.problem.detail
            : 'Não foi possível iniciar agora. Nada mudou. Tente de novo.',
      );
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-[20px] font-bold tracking-[-0.01em]">Iniciar no Paper</h1>
      {doc ? (
        <div className="flex flex-col gap-2 rounded-xl border border-border-strong bg-surface-2 px-3.5 py-3">
          <span className="flex flex-wrap items-center gap-2">
            <ModeBadge mode="paper" />
            Pares: <strong>{doc.pairs.symbols.join(', ')}</strong> · vela de{' '}
            <strong>{doc.pairs.timeframe}</strong>
          </span>
          <span className="text-muted">
            Capital simulado de {formatNumber(settings.data?.capital_reference ?? 0, 0)} USDT.
            Nenhum dinheiro real é usado.
          </span>
        </div>
      ) : (
        <Skeleton className="h-16" aria-busy="true" />
      )}
      <p
        role="note"
        className="flex items-start gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-3 text-sec"
      >
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
        Se o calendário de eventos ficar desligado, este teste nunca poderá liberar o Live.
      </p>
      <p className="text-muted">
        Ficar parado em USDT é normal. O robô só entra quando o risco compensa.
      </p>
      {error ? (
        <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
          <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap justify-between gap-2.5">
        <Button onClick={onBack}>Voltar</Button>
        <Button variant="primary" disabled={pending || !doc} onClick={() => void start()}>
          {pending ? 'Iniciando…' : 'Iniciar no Paper'}
        </Button>
      </div>
    </div>
  );
}
