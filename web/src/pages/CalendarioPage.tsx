import { CalendarClock, TriangleAlert, WifiOff } from 'lucide-react';
import type { Calendar } from '@/api/calendarSchemas';
import { CardError } from '@/components/kit/CardError';
import { EmptyState } from '@/components/kit/EmptyState';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { Hero, type HeroTone } from '@/components/kit/Hero';
import { PageHeader } from '@/components/layout/PageHeader';
import { Skeleton } from '@/components/ui/skeleton';
import { daysText, pastMoment, userZone, validUntilText, type CalendarView } from '@/lib/calendar';
import { formatClock } from '@/lib/format';
import { cn } from '@/lib/utils';
import { CalendarEventItem } from './calendario/CalendarEventItem';
import { RobotPausedCard } from './calendario/RobotPausedCard';
import { useCalendario } from './calendario/useCalendario';

type Seal = { tone: HeroTone; title: string; text: string };

function sealOf(
  view: CalendarView,
  m: Pick<ReturnType<typeof useCalendario>, 'data' | 'active' | 'nowMs' | 'staleSince'>,
): Seal {
  const { data, active, nowMs, staleSince } = m;
  const n = data?.valid_until
    ? Math.max(0, Math.floor((Date.parse(data.valid_until) - nowMs) / 86_400_000))
    : 0;
  if (view === 'ok')
    return {
      tone: 'ok',
      title: 'Calendário em dia',
      text: `Calendário válido por mais ${daysText(n)}. Nenhuma ação sua é necessária.`,
    };
  if (view === 'attention')
    return staleSince !== null && !active
      ? {
          tone: 'warn',
          title: 'Não consegui confirmar o calendário agora',
          text: `Mostro o último estado conhecido: válido por mais ${daysText(n)}. Vou tentar de novo sozinho.`,
        }
      : {
          tone: 'warn',
          title: 'Calendário em dia, com evento em andamento',
          text: `O calendário está válido, mas há um evento em andamento. O robô volta a operar quando a pausa terminar.`,
        };
  if (view === 'expiring')
    return {
      tone: 'warn',
      title:
        n === 0
          ? 'Atenção: calendário vence hoje'
          : n === 1
            ? 'Atenção: calendário vence amanhã'
            : `Atenção: calendário vence em ${n} dias`,
      text: 'Depois disso o robô para de abrir operações. As atuais continuam protegidas. A atualização é automática; se não resolver até lá, avise o suporte.',
    };
  const why =
    data?.last_update.result === 'unchanged' ? 'não encontrou calendário novo' : 'já falhou';
  return {
    tone: 'bad',
    title: 'Calendário vencido',
    text: `Sem calendário válido o robô não abre novas operações. As posições abertas continuam com stop. A atualização é automática e ${why}; chame o suporte.`,
  };
}

function Fact({
  label,
  children,
  detail,
  bad,
}: {
  label: string;
  children: React.ReactNode;
  detail?: React.ReactNode;
  bad?: boolean;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-card border border-border bg-surface p-3.5 shadow-card sm:px-[18px] sm:py-4">
      <dt className="text-[12px] text-muted">{label}</dt>
      <dd className="m-0 flex min-w-0 flex-col gap-0.5">
        <span className="break-words text-[16px] font-semibold">{children}</span>
        {detail ? (
          <span
            className={`text-sec ${cn('break-words', bad ? 'font-semibold text-loss' : 'text-muted')}`}
          >
            {detail}
          </span>
        ) : null}
      </dd>
    </div>
  );
}

function updateFact(c: Calendar, nowMs: number, expired: boolean) {
  const { result, attempted_at: at } = c.last_update;
  if (!at || result === 'never')
    return { title: 'Ainda não houve tentativa automática.', detail: null, failed: false };
  const when = pastMoment(at, nowMs);
  if (result === 'updated') return { title: `Atualizado ${when}`, detail: null, failed: false };
  if (result === 'unchanged')
    return {
      title: `Verificado ${when}`,
      detail: 'Sem mudanças: o calendário já estava em dia.',
      failed: false,
    };
  const last =
    expired && c.last_success_at
      ? ` Última atualização bem-sucedida: ${validUntilText(c.last_success_at)}.`
      : '';
  return {
    title: `Tentativa ${when} falhou`,
    detail: `Nova tentativa automática em breve.${last}`,
    failed: true,
  };
}

function overdueText(days: number | null) {
  if (days === null) return 'Sem calendário válido';
  return days === 0 ? 'Venceu hoje' : `Venceu há ${daysText(days)}`;
}

function Loaded({
  m,
  data,
  view,
}: {
  m: ReturnType<typeof useCalendario>;
  data: Calendar;
  view: CalendarView;
}) {
  const seal = sealOf(view, m);
  const expired = view === 'expired';
  const update = updateFact(data, m.nowMs, expired);
  const daysLeft = data.valid_until
    ? Math.max(0, Math.floor((Date.parse(data.valid_until) - m.nowMs) / 86_400_000))
    : 0;

  return (
    <div className="flex max-w-[880px] flex-col gap-4 lg:gap-5">
      {m.staleSince ? (
        <div
          role="status"
          className="flex items-center gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-2.5 text-sec"
        >
          <WifiOff className="size-4 shrink-0 text-warn" aria-hidden="true" />
          Mostrando dados de {formatClock(m.staleSince).slice(0, 5)}. Sem conexão no momento.
        </div>
      ) : null}

      <div role="status">
        <Hero headingId="calendario-selo" tone={seal.tone} title={seal.title}>
          {seal.text}
        </Hero>
      </div>

      <RobotPausedCard expired={expired} active={m.active} next={m.next} nowMs={m.nowMs} />

      <dl className="m-0 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data.valid_until ? (
          <Fact
            label="Calendário vale até"
            bad={expired}
            detail={
              expired ? (
                <span className="inline-flex items-center gap-1.5">
                  <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
                  {overdueText(m.overdueDays)}
                </span>
              ) : (
                `${daysText(daysLeft)} ${daysLeft === 1 ? 'restante' : 'restantes'}`
              )
            }
          >
            {validUntilText(data.valid_until)}
          </Fact>
        ) : (
          <Fact label="Calendário vale até" detail="Não há calendário carregado.">
            —
          </Fact>
        )}
        <Fact
          label="Última atualização automática"
          detail={
            update.detail ? (
              <span className="inline-flex items-start gap-1.5">
                {update.failed ? (
                  <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
                ) : null}
                {update.detail}
              </span>
            ) : undefined
          }
        >
          {update.title}
        </Fact>
        <Fact label="Seu fuso" detail="Horários convertidos para você">
          {userZone()}
        </Fact>
      </dl>

      <section aria-labelledby="eventos-t" className="flex flex-col gap-3">
        <div>
          <h2 id="eventos-t" className="text-[18px] font-bold">
            Próximos eventos
          </h2>
          <p className="text-muted">
            Em cada um, o robô para antes e só volta depois da{' '}
            <GlossaryTerm term="janela-bloqueio">janela</GlossaryTerm>.
          </p>
        </div>
        {m.events.length === 0 ? (
          <EmptyState
            icon={CalendarClock}
            title={!expired ? 'Nenhum evento nos próximos 90 dias.' : 'Nenhum evento para mostrar.'}
          >
            {!expired
              ? null
              : 'Sem calendário válido o robô não opera. Assim que ele for atualizado, os eventos aparecem aqui.'}
          </EmptyState>
        ) : (
          <ol className="m-0 flex list-none flex-col gap-2.5 p-0">
            {m.events.slice(0, 10).map((e) => (
              <CalendarEventItem key={`${e.start}-${e.label}`} event={e} nowMs={m.nowMs} />
            ))}
          </ol>
        )}
      </section>

      <details className="rounded-card border border-border bg-surface shadow-card">
        <summary className="flex min-h-11 cursor-pointer items-center px-4 font-semibold">
          Por que o robô precisa deste calendário?
        </summary>
        <p className="px-4 pb-4">
          Em horários de notícias fortes o preço dá saltos que passam por cima do{' '}
          <GlossaryTerm term="stop">stop</GlossaryTerm>. Por isso o robô não abre operações perto
          deles. Sem <GlossaryTerm term="calendario">calendário</GlossaryTerm> em dia, ele não sabe
          quando as notícias saem, então prefere não operar.
        </p>
      </details>
    </div>
  );
}

export default function CalendarioPage() {
  const m = useCalendario();
  const { data } = m;

  return (
    <>
      <PageHeader
        title="Calendário de eventos"
        description="O robô fica parado perto de notícias que sacodem o mercado. Aqui você vê quando."
      />

      {data && m.view ? (
        <Loaded m={m} data={data} view={m.view} />
      ) : m.query.isError ? (
        <CardError
          error={m.query.error}
          message="Não consegui carregar o calendário. O robô continua seguindo as regras de segurança. Tente de novo."
          onRetry={() => void m.query.refetch()}
          retrying={m.query.isFetching}
        />
      ) : (
        <div className="flex max-w-[880px] flex-col gap-4" aria-busy="true">
          <Skeleton className="h-24" />
          <Skeleton className="h-20" />
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
            <Skeleton className="h-20 max-lg:hidden" />
          </div>
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
      )}
    </>
  );
}
