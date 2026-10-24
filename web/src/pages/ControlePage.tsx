import { useQueryClient } from '@tanstack/react-query';
import {
  CircleCheck,
  History,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import { useRef, useState, type ReactNode } from 'react';
import { KEYS, useBotStatus, useCommands } from '@/api/queries';
import type { BotStatus } from '@/api/schemas';
import { useAuth } from '@/auth/AuthContext';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { CommandList } from '@/components/kit/CommandList';
import { EmptyState } from '@/components/kit/EmptyState';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { OperatorDialog } from '@/components/kit/OperatorDialog';
import { RiskLimitsList } from '@/components/kit/RiskLimitsList';
import { StateHero } from '@/components/kit/StateHero';
import { StopAllButton } from '@/components/kit/StopAll';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { formatClock } from '@/lib/format';
import { cn } from '@/lib/utils';
import { useConnection } from '@/realtime/ConnectionProvider';
import { LiveGateCard } from './controle/LiveGateCard';
import { StatesTable } from './controle/StatesTable';
import { useControlActions, type ControlAction } from './controle/useControlActions';

const ON_STATES: BotStatus['state'][] = ['STARTING', 'RUNNING', 'PAUSED', 'FAILED_PERSISTENCE'];

const DIALOGS: Record<
  ControlAction,
  { title: string; description: string; confirm: string; tone?: 'danger' }
> = {
  pause: {
    title: 'Pausar novas entradas?',
    description: 'O robô para de abrir operações. As posições abertas seguem protegidas por stop.',
    confirm: 'Pausar novas entradas',
  },
  resume: {
    title: 'Retomar o robô?',
    description: 'O robô volta a procurar oportunidades e a abrir operações dentro dos limites.',
    confirm: 'Retomar',
  },
  reactivate: {
    title: 'Você revisou o que aconteceu?',
    description:
      'O robô desligou porque a queda chegou ao limite e só você pode religar. Será pedido o código do aplicativo autenticador.',
    confirm: 'Reativar o robô',
  },
  'power-on': {
    title: 'Ligar o robô?',
    description: 'O robô volta a operar dentro dos limites de risco.',
    confirm: 'Ligar',
  },
  'power-off': {
    title: 'Desligar o robô?',
    description:
      'Parada ordenada: nenhuma nova operação é aberta. As posições abertas seguem protegidas por stop.',
    confirm: 'Desligar',
    tone: 'danger',
  },
};

function HealthItem({
  label,
  ok,
  attention,
  children,
}: {
  label: ReactNode;
  ok: boolean;
  attention?: boolean;
  children: ReactNode;
}) {
  const Icon: LucideIcon = ok ? CircleCheck : TriangleAlert;
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border py-2.5 last:border-0">
      <span className="text-muted">{label}</span>
      <b
        className={cn(
          'inline-flex items-center gap-1.5',
          ok ? 'text-gain' : attention ? 'text-warn' : 'text-loss',
        )}
      >
        <Icon className="size-4" aria-hidden="true" />
        {children}
      </b>
    </div>
  );
}

function HealthCard({ s }: { s: BotStatus }) {
  const age = s.heartbeat_age_seconds;
  const beatOk = age !== null && age < 90;
  const allOk =
    !s.stops_blind &&
    s.outbox_pending === 0 &&
    !s.persistence_error &&
    beatOk &&
    !s.allow_open_calendar &&
    s.redis_healthy !== false;
  return (
    <Card aria-labelledby="saude-t">
      <CardHeader>
        <CardTitle id="saude-t">
          <ShieldCheck className="size-5" aria-hidden="true" /> Saúde da conexão
        </CardTitle>
        <Chip tone={allOk ? 'ok' : 'warn'}>{allOk ? 'Tudo ok' : 'Atenção'}</Chip>
      </CardHeader>
      <div>
        <HealthItem label="Dados de mercado" ok={!s.stops_blind}>
          {s.stops_blind ? 'Sem preços' : 'Recebendo'}
        </HealthItem>
        <HealthItem
          label={<GlossaryTerm term="stops-cegos">Stops cegos</GlossaryTerm>}
          ok={!s.stops_blind}
        >
          {s.stops_blind ? 'Sim' : 'Não'}
        </HealthItem>
        <HealthItem label="Gravação no banco" ok={s.outbox_pending === 0 && !s.persistence_error}>
          {s.persistence_error
            ? 'Com falha (eventos guardados)'
            : s.outbox_pending === 0
              ? 'Em dia (0 na fila)'
              : `${s.outbox_pending} na fila`}
        </HealthItem>
        <HealthItem label="Batimento do robô" ok={beatOk} attention>
          {age === null ? 'sem sinal' : `há ${Math.round(age)} s`}
        </HealthItem>
        <HealthItem
          label={<GlossaryTerm term="calendario-aberto">Calendário de eventos</GlossaryTerm>}
          ok={!s.allow_open_calendar}
          attention
        >
          {s.allow_open_calendar ? 'Aberto (sem cobertura)' : 'Ligado'}
        </HealthItem>
        <HealthItem label="Comunicação com o robô" ok={s.redis_healthy !== false}>
          {s.redis_healthy === null ? 'Sem informação' : s.redis_healthy ? 'Em dia' : 'Com falha'}
        </HealthItem>
      </div>
    </Card>
  );
}

export default function ControlePage() {
  const qc = useQueryClient();
  const { session } = useAuth();
  const { disconnected } = useConnection();
  const status = useBotStatus();
  const commands = useCommands(8);
  const actions = useControlActions();
  const [dialog, setDialog] = useState<ControlAction | null>(null);
  const [last, setLast] = useState<ControlAction>('pause');
  const [openCount, setOpenCount] = useState(0);
  const returnRef = useRef<HTMLElement | null>(null);

  const s = status.data;
  const isAdmin = session?.user.role === 'admin';
  const blocked = !isAdmin
    ? 'Somente administradores podem mudar o robô. Você pode ver tudo, mas as ações ficam desabilitadas.'
    : disconnected
      ? 'Sem conexão com o painel: as ações ficam desabilitadas até reconectar. Parar tudo continua disponível.'
      : null;
  const locked = blocked !== null;

  const show = (action: ControlAction, trigger: HTMLElement | null) => {
    returnRef.current = trigger;
    setOpenCount((n) => n + 1);
    setLast(action);
    setDialog(action);
  };

  const isOn = !!s && ON_STATES.includes(s.state);
  const halted = s?.state === 'HALTED';
  const canPause = !!s && s.state === 'RUNNING';
  const canResume = !!s && s.state === 'PAUSED';
  const dialogCopy = DIALOGS[last];

  const submit = (action: ControlAction) => (input: { operator: string; reason: string }) => {
    switch (action) {
      case 'pause':
        return actions.pause(input);
      case 'resume':
        return actions.resume(input);
      case 'reactivate':
        return actions.reactivate(input);
      case 'power-on':
        return actions.power(true, input);
      case 'power-off':
        return actions.power(false, input);
    }
  };

  return (
    <>
      <PageHeader
        title="Controle do robô"
        description="Ligar, pausar, parar e entender por que o robô faz o que faz."
      />

      {s ? (
        <StateHero status={s} page="controle" />
      ) : status.isError ? (
        <CardError
          error={status.error}
          onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.status })}
          updatedAt={status.dataUpdatedAt || undefined}
        />
      ) : (
        <Skeleton className="h-[92px] rounded-card" />
      )}

      <section className="grid gap-4 lg:grid-cols-2">
        <Card aria-labelledby="liga-t">
          <CardHeader>
            <CardTitle id="liga-t">
              <SlidersHorizontal className="size-5" aria-hidden="true" /> Liga / desliga
            </CardTitle>
            {s ? <ModeBadge mode={s.mode} hintOnMobile={false} /> : null}
          </CardHeader>
          {s ? (
            <>
              <div className="flex items-center gap-3.5">
                <Switch
                  id="power-switch"
                  checked={isOn}
                  disabled={locked || halted}
                  aria-describedby="power-help"
                  onCheckedChange={(next) =>
                    show(next ? 'power-on' : 'power-off', document.getElementById('power-switch'))
                  }
                />
                <label htmlFor="power-switch" className="flex flex-col font-semibold">
                  {halted ? 'Robô desligado por drawdown' : isOn ? 'Robô ligado' : 'Robô desligado'}
                  <span id="power-help" className="text-sec font-normal text-muted">
                    {halted
                      ? 'Só volta com Reativar, abaixo.'
                      : isOn
                        ? 'Desligar para de abrir novas operações; posições abertas seguem protegidas.'
                        : 'Ligar faz o robô voltar a procurar oportunidades.'}
                  </span>
                </label>
              </div>
              <div className="flex flex-wrap gap-2.5">
                <Button
                  disabled={locked || !canPause}
                  onClick={(e) => show('pause', e.currentTarget)}
                >
                  <Pause className="size-4" aria-hidden="true" /> Pausar novas entradas
                </Button>
                <Button
                  variant="ghost"
                  disabled={locked || !canResume}
                  onClick={(e) => show('resume', e.currentTarget)}
                >
                  <Play className="size-4" aria-hidden="true" />
                  {canResume ? 'Retomar' : 'Retomar (nada pausado)'}
                </Button>
                {halted ? (
                  <Button
                    variant="primary"
                    disabled={locked}
                    onClick={(e) => show('reactivate', e.currentTarget)}
                  >
                    <RotateCcw className="size-4" aria-hidden="true" /> Reativar
                  </Button>
                ) : null}
              </div>
              {blocked ? (
                <p role="note" className="text-sec text-muted">
                  {blocked}
                </p>
              ) : null}
              <h3 className="mt-1.5 text-card font-bold">Pausas ativas</h3>
              {s.pause_reasons.length > 0 ? (
                <ul className="flex flex-col gap-2">
                  {s.pause_reasons.map((p) => (
                    <li
                      key={`${p.at}-${p.reason}`}
                      className="rounded-xl border border-warn bg-warn-bg px-3 py-2.5"
                    >
                      <strong>{p.reason}</strong>
                      <span className="block text-sec text-muted">
                        {p.operator} às {formatClock(p.at).slice(0, 5)}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <EmptyState icon={CircleCheck} title="Nenhuma pausa ativa">
                  Quando houver, o motivo e quem pausou aparecem aqui.
                </EmptyState>
              )}
            </>
          ) : status.isError ? (
            <CardError
              error={status.error}
              onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.status })}
            />
          ) : (
            <div className="flex flex-col gap-3" aria-busy="true">
              <Skeleton className="h-10" />
              <Skeleton className="h-11" />
              <Skeleton className="h-20" />
            </div>
          )}
        </Card>

        {s ? (
          <HealthCard s={s} />
        ) : (
          <Card aria-busy="true">
            <Skeleton className="h-6 w-1/2" />
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-8" />
            ))}
          </Card>
        )}
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Card aria-labelledby="travas-c-t">
          <CardHeader>
            <CardTitle id="travas-c-t">
              <ShieldCheck className="size-5" aria-hidden="true" /> Travas de risco
            </CardTitle>
          </CardHeader>
          {s ? (
            <RiskLimitsList limits={s.limits} />
          ) : (
            <div className="flex flex-col gap-4" aria-busy="true">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-16" />
              ))}
            </div>
          )}
        </Card>

        <Card aria-labelledby="parar-t" className="self-start border-loss">
          <CardHeader>
            <CardTitle id="parar-t" className="text-loss">
              <Square className="size-5 fill-current" aria-hidden="true" /> Parar tudo (emergência)
            </CardTitle>
          </CardHeader>
          <p>
            Use quando algo estiver errado. Bloqueia novas operações na hora. Você escolhe o que
            fazer com o que está aberto: só bloquear (recomendado) ou vender tudo a mercado.
          </p>
          <div>
            <StopAllButton
              mode={s?.mode ?? null}
              operatorName={session?.user.name}
              canStop={isAdmin}
              label="Parar tudo agora"
              className="ml-0"
            />
          </div>
          {!isAdmin ? (
            <p role="note" className="text-sec text-muted">
              Somente administradores podem parar o robô.
            </p>
          ) : null}
        </Card>
      </section>

      <LiveGateCard isAdmin={isAdmin} allowOpenCalendar={s?.allow_open_calendar ?? false} />

      <section className="grid gap-4 lg:grid-cols-2">
        <StatesTable />
        <Card aria-labelledby="hist-t" className="lg:col-span-2">
          <CardTitle id="hist-t">
            <History className="size-5" aria-hidden="true" /> Histórico de controle
          </CardTitle>
          {commands.data ? (
            commands.data.items.length > 0 ? (
              <CommandList items={commands.data.items} />
            ) : (
              <EmptyState icon={History} title="Nenhum comando enviado ainda">
                Pausas, retomadas e paradas aparecem aqui, com o estado de cada uma.
              </EmptyState>
            )
          ) : commands.isError ? (
            <CardError
              error={commands.error}
              onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.commands })}
            />
          ) : (
            <div className="flex flex-col gap-3" aria-busy="true">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          )}
        </Card>
      </section>

      <OperatorDialog
        key={openCount}
        open={dialog !== null}
        onOpenChange={(next) => {
          if (!next) setDialog(null);
        }}
        title={dialogCopy.title}
        description={dialogCopy.description}
        confirmLabel={dialogCopy.confirm}
        tone={dialogCopy.tone ?? 'primary'}
        defaultOperator={session?.user.name ?? ''}
        returnFocusRef={returnRef}
        onSubmit={submit(last)}
      />
    </>
  );
}
