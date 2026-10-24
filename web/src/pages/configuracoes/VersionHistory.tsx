import { History } from 'lucide-react';
import { useSettingsHistory } from '@/api/settingsQueries';
import type { SettingsVersion } from '@/api/settingsSchemas';
import { CardError } from '@/components/kit/CardError';
import { Chip, type ChipTone } from '@/components/kit/Chip';
import { EmptyState } from '@/components/kit/EmptyState';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';

const SECTION_LABEL: Record<string, string> = {
  pairs: 'Pares',
  strategies: 'Estratégias',
  risk: 'Risco',
  ai: 'IA',
};

const APPLICATION_CHIP: Record<
  SettingsVersion['application'],
  { label: string; tone: ChipTone } | null
> = {
  none: null,
  pending: { label: 'Aguardando o robô', tone: 'info' },
  applied: { label: 'Aplicado ao robô', tone: 'ok' },
  partially_applied: { label: 'Aplicado em parte', tone: 'warn' },
  refused: { label: 'Recusado pelo robô', tone: 'bad' },
  expired: { label: 'Expirou', tone: 'warn' },
  failed: { label: 'Falhou', tone: 'bad' },
};

const when = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

/** Como a versao vale no robo, em palavras: chip + frase do servidor (nunca so cor). */
export function ApplicationState({ version }: { version: SettingsVersion }) {
  const chip = APPLICATION_CHIP[version.application];
  const nextStart =
    version.application === 'none' && version.activation === 'next_start'
      ? { label: 'Vale no próximo início', tone: 'off' as const }
      : null;
  const shown = chip ?? nextStart;
  return (
    <span className="flex flex-wrap items-center gap-2">
      {shown ? <Chip tone={shown.tone}>{shown.label}</Chip> : null}
      {version.application_text ? (
        <span className="text-sec text-muted">{version.application_text}</span>
      ) : null}
    </span>
  );
}

function VersionItem({ v }: { v: SettingsVersion }) {
  return (
    <li className="flex flex-col gap-2 border-b border-border py-3.5 first:pt-0 last:border-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <strong>Versão {v.id}</strong>
        <span className="text-sec text-muted">
          {when(v.created_at)} por {v.actor}
        </span>
        {v.sections.map((s) => (
          <Chip key={s} tone="off" icon={false}>
            {SECTION_LABEL[s] ?? s}
          </Chip>
        ))}
        {v.risk_increase ? <Chip tone="warn">Aumentou o risco</Chip> : null}
      </div>
      <p>
        <span className="text-muted">Motivo: </span>
        {v.reason}
      </p>
      <ApplicationState version={v} />
      {v.changes.length > 0 ? (
        <details>
          <summary className="min-h-11 cursor-pointer py-2.5 font-semibold">
            Ver o que mudou ({v.changes.length})
          </summary>
          <ul className="flex flex-col gap-1.5 text-sec">
            {v.changes.map((c) => (
              <li key={c.path}>
                <strong>{c.label}:</strong> {c.before_text} → {c.after_text}
                {c.increases_risk
                  ? ' (aumenta o risco)'
                  : c.effect === 'decrease'
                    ? ' (reduz o risco)'
                    : ''}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </li>
  );
}

export function VersionHistory() {
  const history = useSettingsHistory();
  const items = history.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <Card aria-labelledby="ver-t">
      <CardHeader>
        <CardTitle id="ver-t">
          <History className="size-5" aria-hidden="true" /> Histórico de versões
        </CardTitle>
      </CardHeader>
      {history.isError ? (
        <CardError
          error={history.error}
          onRetry={() => void history.refetch()}
          retrying={history.isFetching}
        />
      ) : history.isPending ? (
        <Skeleton className="h-16" aria-busy="true" />
      ) : items.length === 0 ? (
        <EmptyState title="Nenhuma mudança ainda">
          Quando alguém salvar uma configuração, ela aparece aqui com autor, motivo e o que mudou.
        </EmptyState>
      ) : (
        <>
          <ul aria-label="Versões da configuração">
            {items.map((v) => (
              <VersionItem key={v.id} v={v} />
            ))}
          </ul>
          {history.hasNextPage ? (
            <div>
              <Button
                disabled={history.isFetchingNextPage}
                onClick={() => void history.fetchNextPage()}
              >
                Mostrar mais antigas
              </Button>
            </div>
          ) : null}
        </>
      )}
    </Card>
  );
}
