import { RefreshCw, TriangleAlert } from 'lucide-react';
import { ApiError } from '@/api/http';
import { Button } from '@/components/ui/button';
import { formatClock } from '@/lib/format';

/** Erro de dados isolado no card: o resto da tela segue vivo (wireframes 0.3). */
export function CardError({
  error,
  onRetry,
  updatedAt,
  retrying,
}: {
  error: unknown;
  onRetry: () => void;
  updatedAt?: number;
  retrying?: boolean;
}) {
  const text =
    error instanceof ApiError && error.isNetwork
      ? 'Sem conexão com o servidor.'
      : 'Não foi possível carregar estes dados.';
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-warn bg-warn-bg px-3.5 py-3"
    >
      <p className="flex items-start gap-2 text-sec">
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
        <span>
          {text}
          {updatedAt ? ` Última atualização às ${formatClock(updatedAt)}.` : ''}
        </span>
      </p>
      <Button onClick={onRetry} disabled={retrying}>
        <RefreshCw className="size-4" aria-hidden="true" /> Tentar de novo
      </Button>
    </div>
  );
}
