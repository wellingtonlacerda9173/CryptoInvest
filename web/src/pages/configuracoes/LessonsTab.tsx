import { useQueryClient } from '@tanstack/react-query';
import { BookOpenCheck, CircleCheck, CircleX, Clock, Info, Play, RefreshCw } from 'lucide-react';
import { useId, useRef, useState } from 'react';
import { AI_KEYS, aiApi, useAiLesson, useAiLessons } from '@/api/aiQueries';
import type { Lesson, LessonStatus } from '@/api/aiSchemas';
import { ApiError } from '@/api/http';
import { ReauthCancelled, useFreshAuth } from '@/auth/ReauthContext';
import { useAuth } from '@/auth/AuthContext';
import { CardError } from '@/components/kit/CardError';
import { Chip, type ChipTone } from '@/components/kit/Chip';
import { ConfirmDialog } from '@/components/kit/ConfirmDialog';
import { EmptyState } from '@/components/kit/EmptyState';
import { notify } from '@/components/kit/Toaster';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { formatSigned } from '@/lib/format';
import { cn } from '@/lib/utils';

const STATUS: Record<LessonStatus, { tone: ChipTone; label: string; icon: typeof Clock }> = {
  pending: { tone: 'off', label: 'Pendente de revisão', icon: Clock },
  approved: { tone: 'ok', label: 'Aprovada', icon: CircleCheck },
  rejected: { tone: 'bad', label: 'Rejeitada', icon: CircleX },
};
const CONFIDENCE = { low: 'baixa', medium: 'média', high: 'alta' } as const;

const stamp = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

function failure(error: unknown, what: string): string {
  if (error instanceof ReauthCancelled) return `Confirmação cancelada. ${what}`;
  if (error instanceof ApiError) {
    if (error.isNetwork) return `Sem conexão com o servidor. ${what}`;
    if (error.problem.detail) return error.problem.detail;
  }
  return `Não foi possível concluir agora. ${what}`;
}

function StatusChip({ status }: { status: LessonStatus }) {
  const { tone, label, icon } = STATUS[status];
  return (
    <Chip tone={tone} icon={icon}>
      {label}
    </Chip>
  );
}

function LessonDetail({ id, canReview }: { id: string; canReview: boolean }) {
  const qc = useQueryClient();
  const runFresh = useFreshAuth();
  const lesson = useAiLesson(id);
  const reasonId = useId();
  const noteId = useId();
  const [decision, setDecision] = useState<'approve' | 'reject' | null>(null);
  const [reason, setReason] = useState('');
  const returnRef = useRef<HTMLButtonElement>(null);
  const l = lesson.data;

  if (lesson.isError)
    return (
      <CardError
        error={lesson.error}
        onRetry={() => void lesson.refetch()}
        retrying={lesson.isFetching}
      />
    );
  if (!l)
    return (
      <div aria-busy="true" className="flex flex-col gap-2">
        <Skeleton className="h-6" />
        <Skeleton className="h-24" />
      </div>
    );

  const verb = decision === 'approve' ? 'Aprovar' : 'Rejeitar';
  return (
    <article aria-label="Detalhe da lição" className="flex flex-col gap-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <StatusChip status={l.status} />
        <Chip tone="info" icon={false}>
          {l.source === 'ai' ? 'Texto escrito pela IA' : 'Só estatísticas (sem IA)'}
        </Chip>
        <span className="text-sec text-muted">
          Período {stamp(l.period_start)} a {stamp(l.period_end)}
        </span>
      </div>
      <p className="font-semibold">{l.summary}</p>
      <ul className="flex flex-col gap-2.5" aria-label="Observações">
        {l.observations.map((o, index) => (
          <li key={`${index}|${o.text}|${o.evidence}`} className="rounded-xl bg-surface-2 p-3">
            <span className="block">{o.text}</span>
            <span className="block text-sec text-muted tabular-nums">Evidência: {o.evidence}</span>
            <span className="block text-cap text-muted">Confiança {CONFIDENCE[o.confidence]}</span>
          </li>
        ))}
      </ul>
      <section aria-label="Números do período" className="flex flex-col gap-1.5">
        <p className="tabular-nums text-sec">
          {l.stats.trades} operações: {l.stats.wins} ganhos e{' '}
          <strong>{l.stats.losses} perdas</strong>, resultado {formatSigned(l.stats.total_r)} R
          (perdas {formatSigned(l.stats.loss_r)} R). {l.stats.skipped} sinais não operados.
        </p>
        {l.stats.by_strategy.length > 0 ? (
          <ul className="flex flex-col gap-0.5 text-sec tabular-nums">
            {l.stats.by_strategy.map((g) => (
              <li key={g.key}>
                {g.key}: {g.trades} operações, {g.losses} perdas, total {formatSigned(g.total_r)} R,
                pior {formatSigned(g.worst_r)} R
              </li>
            ))}
          </ul>
        ) : null}
        {l.stats.worst_losses.length > 0 ? (
          <p className="text-sec text-muted tabular-nums">
            Maiores perdas:{' '}
            {l.stats.worst_losses
              .map((w) => `${w.symbol} ${formatSigned(w.r_multiple)} R`)
              .join(' · ')}
          </p>
        ) : null}
      </section>
      {l.reviewer ? (
        <p className="text-sec text-muted">
          {STATUS[l.status].label} por {l.reviewer}
          {l.reviewed_at ? ` em ${stamp(l.reviewed_at)}` : ''}: {l.review_reason}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          ref={returnRef}
          variant="primary"
          disabled={!canReview || l.status === 'approved'}
          aria-describedby={canReview ? undefined : noteId}
          onClick={() => setDecision('approve')}
        >
          Aprovar
        </Button>
        <Button
          disabled={!canReview || l.status === 'rejected'}
          aria-describedby={canReview ? undefined : noteId}
          onClick={() => setDecision('reject')}
        >
          Rejeitar
        </Button>
        {canReview ? null : (
          <span id={noteId} className="text-sec text-muted">
            Só administradores aprovam ou rejeitam lições. Você pode ler e acompanhar.
          </span>
        )}
      </div>
      <ConfirmDialog
        open={decision !== null}
        onOpenChange={(open) => {
          if (!open) {
            setDecision(null);
            setReason('');
          }
        }}
        returnFocusRef={returnRef}
        title={`${verb} esta lição?`}
        description={
          decision === 'approve'
            ? 'Depois de aprovada, o texto entra como contexto de leitura nas análises da IA. Ele nunca muda regras de risco. Você confirma com o código do aplicativo.'
            : 'Lição rejeitada nunca é enviada à IA.'
        }
        confirmLabel={verb}
        confirmDisabled={reason.trim().length < 3}
        onConfirm={async () => {
          if (decision === null) return;
          try {
            await runFresh(() => aiApi.review(l.id, { decision, reason: reason.trim() }));
          } catch (cause) {
            throw new Error(failure(cause, 'A lição não foi alterada.'), { cause });
          }
          await qc.invalidateQueries({ queryKey: AI_KEYS.lessons });
          notify.success(decision === 'approve' ? 'Lição aprovada' : 'Lição rejeitada');
        }}
      >
        <Field label="Motivo (obrigatório)" htmlFor={reasonId}>
          <Input
            id={reasonId}
            value={reason}
            maxLength={500}
            autoComplete="off"
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
      </ConfirmDialog>
    </article>
  );
}

function LessonRow({
  lesson,
  selected,
  onSelect,
}: {
  lesson: Lesson;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        aria-pressed={selected}
        onClick={onSelect}
        className={cn(
          'flex w-full cursor-pointer flex-col items-start gap-1 rounded-xl border bg-transparent px-3 py-2.5 text-left text-text hover:bg-surface-2',
          selected ? 'border-info bg-surface-2' : 'border-border',
        )}
      >
        <span className="flex flex-wrap items-center gap-2">
          <time dateTime={lesson.created_at} className="font-semibold tabular-nums">
            {stamp(lesson.created_at)}
          </time>
          <StatusChip status={lesson.status} />
        </span>
        <span className="line-clamp-2 text-sec text-muted">{lesson.summary}</span>
      </button>
    </li>
  );
}

export function LessonsTab() {
  const qc = useQueryClient();
  const { session } = useAuth();
  const canReview = session?.user.role === 'admin';
  const lessons = useAiLessons();
  const [picked, setPicked] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const items = lessons.data?.pages.flatMap((p) => p.items) ?? [];
  const selected = picked ?? items[0]?.id ?? null;

  const run = async () => {
    setRunning(true);
    try {
      const out = await aiApi.run();
      notify.success('Gerando lições', out.message);
      await qc.invalidateQueries({ queryKey: AI_KEYS.lessons });
    } catch (cause) {
      notify.error('Não foi possível gerar', failure(cause, 'Tente de novo em instantes.'));
    } finally {
      setRunning(false);
    }
  };

  return (
    <section aria-labelledby="lic-t" className="flex flex-col gap-3.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 id="lic-t" className="flex items-center gap-2 text-card font-bold">
          <BookOpenCheck className="size-5" aria-hidden="true" /> Lições da IA
        </h3>
        <span className="flex flex-wrap items-center gap-2">
          <Button onClick={() => void lessons.refetch()} disabled={lessons.isFetching}>
            <RefreshCw className="size-4" aria-hidden="true" /> Atualizar
          </Button>
          <Button
            variant="primary"
            disabled={!canReview || running}
            aria-describedby={canReview ? undefined : 'lic-run-note'}
            onClick={() => void run()}
          >
            <Play className="size-4" aria-hidden="true" /> Gerar agora
          </Button>
        </span>
      </div>
      {canReview ? null : (
        <p id="lic-run-note" className="text-sec text-muted">
          Só administradores geram e revisam lições. Você pode ler tudo.
        </p>
      )}
      <p
        role="note"
        className="flex items-start gap-2 rounded-xl border border-info bg-info-bg px-3.5 py-2.5"
      >
        <Info className="mt-0.5 size-4 shrink-0 text-info" aria-hidden="true" />
        <span>
          Lições aprovadas entram como <strong>contexto de leitura</strong> da IA e nunca mudam
          regras de risco, limites, parâmetros ou notas das estratégias. Lições pendentes ou
          rejeitadas não são enviadas à IA. Perdas aparecem sempre.
        </span>
      </p>
      {lessons.isError ? (
        <CardError
          error={lessons.error}
          onRetry={() => void lessons.refetch()}
          retrying={lessons.isFetching}
        />
      ) : lessons.isPending ? (
        <div aria-busy="true" className="flex flex-col gap-2">
          <Skeleton className="h-16" />
          <Skeleton className="h-16" />
        </div>
      ) : items.length === 0 ? (
        <EmptyState icon={BookOpenCheck} title="Ainda não há lições">
          O relatório sai toda semana, ou quando um administrador pede em &quot;Gerar agora&quot;.
        </EmptyState>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,320px)_1fr]">
          <div className="flex flex-col gap-2">
            <ul aria-label="Lições" className="flex flex-col gap-2">
              {items.map((l) => (
                <LessonRow
                  key={l.id}
                  lesson={l}
                  selected={l.id === selected}
                  onSelect={() => setPicked(l.id)}
                />
              ))}
            </ul>
            {lessons.hasNextPage ? (
              <Button
                onClick={() => void lessons.fetchNextPage()}
                disabled={lessons.isFetchingNextPage}
              >
                Ver lições mais antigas
              </Button>
            ) : null}
          </div>
          {selected ? <LessonDetail key={selected} id={selected} canReview={canReview} /> : null}
        </div>
      )}
    </section>
  );
}
