import { TriangleAlert } from 'lucide-react';

/** Persistente e nao fechavel: um run sem calendario valido no periodo nunca libera o Live (wireframes 0.4). */
export function IneligibleSeal({ reason }: { reason?: string | null }) {
  return (
    <div
      role="alert"
      className="flex items-start gap-2.5 rounded-xl border border-warn bg-warn-bg px-3.5 py-3"
    >
      <TriangleAlert className="mt-0.5 size-5 shrink-0 text-warn" aria-hidden="true" />
      <div className="flex flex-col gap-0.5">
        <strong>Teste inelegível para o Live (sem calendário válido no período)</strong>
        <span className="text-sec text-muted">
          {reason ??
            'Este teste rodou sem o calendário de eventos. Ele nunca vai poder liberar dinheiro real; é preciso começar um novo teste com o calendário.'}
        </span>
      </div>
    </div>
  );
}
