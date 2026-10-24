import { WifiOff } from 'lucide-react';
import { formatClock } from '@/lib/format';

export function ConnectionBanner({ lastDataAt }: { lastDataAt: number | null }) {
  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="connection-banner"
      className="flex items-start gap-2.5 border-b border-warn bg-warn-bg px-4 py-2.5 text-sec text-text sm:px-5 lg:px-7"
    >
      <WifiOff className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
      <p>
        <strong>Sem conexão.</strong>{' '}
        {lastDataAt
          ? `Os números abaixo são de ${formatClock(lastDataAt)} e podem estar desatualizados. `
          : 'Os números abaixo podem estar desatualizados. '}
        Tentando reconectar…
      </p>
    </div>
  );
}
