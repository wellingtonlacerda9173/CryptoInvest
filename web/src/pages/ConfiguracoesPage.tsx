import { useQueryClient } from '@tanstack/react-query';
import { CircleCheck, Info, Settings, TriangleAlert } from 'lucide-react';
import { Tabs } from 'radix-ui';
import { Link, useSearchParams } from 'react-router-dom';
import { useState } from 'react';
import { KEYS, useInvalidateAfterCommand } from '@/api/queries';
import {
  SETTINGS_FULL_KEY,
  SETTINGS_HISTORY_KEY,
  useFullSettings,
  useSettingsHistory,
} from '@/api/settingsQueries';
import type { UpdateResult } from '@/api/settingsSchemas';
import { CardError } from '@/components/kit/CardError';
import { PageHeader } from '@/components/layout/PageHeader';
import { buttonVariants } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import { AiValueTab } from './configuracoes/AiValueTab';
import { KeysTab } from './configuracoes/KeysTab';
import { LessonsTab } from './configuracoes/LessonsTab';
import { AiTab, PairsTab, RiskTab, StrategiesTab } from './configuracoes/Tabs';
import { ApplicationState, VersionHistory } from './configuracoes/VersionHistory';

const TRIGGER =
  'min-h-11 shrink-0 cursor-pointer border-b-[3px] border-transparent bg-transparent px-4 font-semibold text-muted data-[state=active]:border-info data-[state=active]:text-text';
const TABS = [
  ['pares', 'Pares'],
  ['estrategias', 'Estratégias'],
  ['risco', 'Risco'],
  ['ia', 'IA'],
  ['licoes', 'Lições da IA'],
  ['valor', 'Valor da IA'],
  ['chaves', 'Conta e chaves'],
] as const;

type Notice = { kind: 'saved'; result: UpdateResult } | { kind: 'conflict'; text: string };

export default function ConfiguracoesPage() {
  const qc = useQueryClient();
  const settings = useFullSettings();
  const history = useSettingsHistory();
  const afterCommand = useInvalidateAfterCommand();
  const [params, setParams] = useSearchParams();
  const [notice, setNotice] = useState<Notice | null>(null);
  const tab = TABS.find(([v]) => v === params.get('aba'))?.[0] ?? 'pares';
  const s = settings.data;

  const reload = () =>
    Promise.all([
      qc.invalidateQueries({ queryKey: SETTINGS_FULL_KEY }),
      qc.invalidateQueries({ queryKey: SETTINGS_HISTORY_KEY }),
      qc.invalidateQueries({ queryKey: KEYS.limits }),
      afterCommand(),
    ]);

  const handlers = {
    onSaved: (result: UpdateResult) => {
      setNotice({ kind: 'saved', result });
      void reload();
    },
    onConflict: (text: string) => {
      setNotice({ kind: 'conflict', text });
      void reload();
    },
  };

  const savedVersion =
    notice?.kind === 'saved'
      ? (history.data?.pages
          .flatMap((p) => p.items)
          .find((v) => v.id === notice.result.version.id) ?? notice.result.version)
      : null;

  return (
    <>
      <PageHeader
        title="Configurações"
        description="Pares, estratégias, risco, IA e conta. Cada mudança fica registrada."
      />

      {notice?.kind === 'conflict' ? (
        <p
          role="alert"
          className="flex items-start gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-2.5"
        >
          <TriangleAlert className="mt-0.5 size-5 shrink-0 text-warn" aria-hidden="true" />
          <span>
            <strong>A configuração mudou enquanto você editava.</strong> Alguém salvou antes de
            você. Recarregamos os valores atuais e a sua edição não foi salva: revise e faça a
            mudança de novo.
          </span>
        </p>
      ) : null}
      {notice?.kind === 'saved' && savedVersion ? (
        <div
          role="status"
          className="flex flex-col gap-1.5 rounded-xl border border-gain bg-gain-bg px-3.5 py-2.5"
        >
          <strong className="flex items-center gap-2">
            <CircleCheck className="size-5 text-gain" aria-hidden="true" /> Versão {savedVersion.id}{' '}
            salva.
          </strong>
          <span>{notice.result.activation_text}</span>
          <ApplicationState version={savedVersion} />
        </div>
      ) : null}
      {s && s.effective.matches_latest === false ? (
        <p
          role="note"
          className="flex items-start gap-2 rounded-xl border border-border-strong bg-surface-2 px-3.5 py-2.5 text-sec"
        >
          <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          Há mudanças salvas aguardando o próximo início do robô. {s.effective.restart_rule}
        </p>
      ) : null}

      <Card aria-labelledby="cfg-t">
        <CardHeader>
          <CardTitle id="cfg-t">
            <Settings className="size-5" aria-hidden="true" /> Ajustes do robô
          </CardTitle>
        </CardHeader>
        {settings.isError ? (
          <CardError
            error={settings.error}
            onRetry={() => void settings.refetch()}
            retrying={settings.isFetching}
          />
        ) : !s ? (
          <div aria-busy="true" className="flex flex-col gap-3">
            <Skeleton className="h-11" />
            <Skeleton className="h-11" />
            <Skeleton className="h-24" />
          </div>
        ) : (
          <Tabs.Root
            value={tab}
            onValueChange={(v) => setParams({ aba: v }, { replace: true })}
            className="flex flex-col gap-4"
          >
            <Tabs.List
              aria-label="Seções das configurações"
              className="flex overflow-x-auto border-b border-border"
            >
              {TABS.map(([v, label]) => (
                <Tabs.Trigger key={v} value={v} className={cn(TRIGGER)}>
                  {label}
                </Tabs.Trigger>
              ))}
            </Tabs.List>
            {/* forceMount: trocar de aba nao joga fora o que a pessoa ja digitou */}
            {(
              [
                ['pares', <PairsTab key={`p${s.version?.id ?? 0}`} settings={s} {...handlers} />],
                [
                  'estrategias',
                  <StrategiesTab key={`e${s.version?.id ?? 0}`} settings={s} {...handlers} />,
                ],
                ['risco', <RiskTab key={`r${s.version?.id ?? 0}`} settings={s} {...handlers} />],
                ['ia', <AiTab key={`i${s.version?.id ?? 0}`} settings={s} {...handlers} />],
              ] as const
            ).map(([v, node]) => (
              <Tabs.Content key={v} value={v} forceMount className="data-[state=inactive]:hidden">
                {node}
              </Tabs.Content>
            ))}
            <Tabs.Content value="licoes">
              <LessonsTab />
            </Tabs.Content>
            <Tabs.Content value="valor">
              <AiValueTab />
            </Tabs.Content>
            <Tabs.Content value="chaves" className="flex flex-col gap-4">
              <KeysTab canEdit={s.can_edit} />
              <p className="text-sec text-muted">
                Senha, sessões e códigos de recuperação ficam em{' '}
                <Link to="/conta" className={buttonVariants({ variant: 'ghost' })}>
                  Minha conta
                </Link>
                .
              </p>
            </Tabs.Content>
          </Tabs.Root>
        )}
      </Card>

      <VersionHistory />
    </>
  );
}
