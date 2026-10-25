import { ArrowUp, ChevronDown, CircleDollarSign, Pause } from 'lucide-react';
import { useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { readSignal } from '@/api/marketSchemas';
import type { Decision } from '@/api/schemas';
import { Chip, type ChipTone } from '@/components/kit/Chip';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { formatNumber } from '@/lib/format';
import { cn } from '@/lib/utils';

const KIND = {
  entered: { tone: 'ok', icon: ArrowUp },
  waited: { tone: 'off', icon: CircleDollarSign },
  refused: { tone: 'warn', icon: Pause },
  info: { tone: 'info', icon: CircleDollarSign },
} as const satisfies Record<Decision['outcome_kind'], { tone: ChipTone; icon: typeof ArrowUp }>;

const stamp = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

const FINAL: Record<Decision['outcome_kind'], string> = {
  entered: 'O robô entrou na operação.',
  waited: 'O robô esperou e ficou parado em USDT.',
  refused: 'O robô viu uma oportunidade, mas não entrou.',
  info: 'Registro informativo do robô.',
};

export function DecisionItem({ decision: d }: { decision: Decision }) {
  const [open, setOpen] = useState(false);
  const panel = useId();
  const { tone, icon } = KIND[d.outcome_kind];
  const insight = readSignal(d.signal);
  const { entry, stop, target } = insight.levels;

  return (
    <li className="border-b border-border last:border-0" data-traded={d.traded}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panel}
        onClick={() => setOpen((v) => !v)}
        className="grid w-full min-w-0 cursor-pointer grid-cols-[84px_1fr_auto] items-start gap-3 bg-transparent px-1 py-3 text-left text-text hover:bg-surface-2 max-sm:grid-cols-[1fr_auto]"
      >
        <time dateTime={d.at} className="text-sec tabular-nums text-muted max-sm:col-span-2">
          {stamp(d.at)}
        </time>
        <span className="min-w-0 max-sm:col-start-1">
          <span className="flex flex-wrap items-center gap-2 font-semibold">
            {d.symbol}
            <Chip tone={tone} icon={icon}>
              {d.outcome}
            </Chip>
          </span>
          <span className="mt-0.5 block text-sec text-muted">{d.reason_text}</span>
        </span>
        <span className="mt-1 inline-flex items-center gap-1 text-cap text-muted">
          <span className="max-sm:sr-only">{open ? 'Ocultar' : 'Detalhes'}</span>
          <ChevronDown
            className={cn('size-4 transition-transform', open && 'rotate-180')}
            aria-hidden="true"
          />
        </span>
      </button>
      {d.code === 'event_calendar_stale' ? (
        <p className="px-1 pb-3 text-sec">
          <Link
            to="/calendario"
            aria-label="Ver o calendário de eventos"
            className="font-semibold underline underline-offset-2"
          >
            Ver calendário
          </Link>
        </p>
      ) : null}
      <div id={panel} hidden={!open} className="px-1 pb-4">
        {open ? (
          <dl className="grid gap-x-4 gap-y-3 rounded-xl bg-surface-2 p-3.5 text-sec sm:grid-cols-[170px_1fr]">
            <dt className="font-semibold">
              <GlossaryTerm term="regime" />
            </dt>
            <dd>
              {insight.regime ?? (
                <span className="text-muted">
                  O robô ainda não registra o regime do mercado nesta decisão.
                </span>
              )}
            </dd>

            <dt className="font-semibold">Estratégias avaliadas</dt>
            <dd>
              {insight.scores.length > 0 ? (
                <ul className="flex flex-col gap-1">
                  {insight.scores.map((s) => (
                    <li key={s.strategy} className="tabular-nums">
                      {s.strategy}: nota {formatNumber(s.score, 2)}
                    </li>
                  ))}
                </ul>
              ) : d.strategy ? (
                <span className="tabular-nums">
                  {d.strategy}
                  {d.confidence !== null ? `: confiança ${formatNumber(d.confidence, 2)}` : ''}
                </span>
              ) : (
                <span className="text-muted">Nenhuma estratégia chegou a propor entrada.</span>
              )}
            </dd>

            {entry !== null && stop !== null && target !== null ? (
              <>
                <dt className="font-semibold">Sinal</dt>
                <dd className="tabular-nums">
                  entrada {formatNumber(entry)} · <GlossaryTerm term="stop" /> {formatNumber(stop)}{' '}
                  · alvo {formatNumber(target)}
                </dd>
              </>
            ) : null}

            <dt className="font-semibold">Parecer da IA</dt>
            <dd className="flex flex-col gap-1">
              {insight.aiDetail ? (
                <span>
                  <Chip
                    tone={insight.aiDetail.verdict === 'veto' ? 'warn' : 'ok'}
                    icon={insight.aiDetail.verdict === 'veto' ? Pause : ArrowUp}
                  >
                    {insight.aiDetail.verdict === 'veto' ? 'IA vetou' : 'IA aprovou'} · confiança{' '}
                    {formatNumber(insight.aiDetail.confidence * 100, 0)}%
                  </Chip>
                </span>
              ) : null}
              {insight.ai ?? (
                <span className="text-muted">
                  Sem parecer da IA nesta decisão. A IA é opcional: quando está desligada ou não foi
                  consultada, o robô decide só pelas regras.
                </span>
              )}
            </dd>

            <dt className="font-semibold">Decisão final</dt>
            <dd>
              <strong>{d.outcome}.</strong> {FINAL[d.outcome_kind]} {d.reason_text}
              {d.code ? (
                <>
                  {' '}
                  <code className="rounded bg-surface px-1 text-cap">{d.code}</code>
                </>
              ) : null}
            </dd>
          </dl>
        ) : null}
      </div>
    </li>
  );
}
