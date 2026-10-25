import { Link } from 'react-router-dom';
import { TriangleAlert } from 'lucide-react';
import { useCalendar } from '@/api/calendarQueries';
import type { BotStatus } from '@/api/schemas';
import { formatClock } from '@/lib/format';
import { Hero, type HeroTone } from './Hero';

interface HeroCopy {
  tone: HeroTone;
  title: string;
  text: string;
  action?: { to: string; label: string };
}

const hhmm = (iso: string) => formatClock(iso).slice(0, 5);

export function heroFor(s: BotStatus, page: 'dashboard' | 'controle'): HeroCopy {
  const openCount = s.open_positions.length;
  const toControl = page === 'dashboard' ? { to: '/controle', label: 'Ir ao Controle' } : undefined;

  if (s.state === 'HALTED')
    return {
      tone: 'bad',
      title: 'O robô desligou porque a queda chegou ao limite.',
      text: `${s.halt_reason ?? 'Ele não volta sozinho.'} Revise o que aconteceu e reative pelo Controle.`,
      action: toControl,
    };
  if (s.state === 'FAILED_PERSISTENCE')
    return {
      tone: 'bad',
      title: 'Falha ao gravar: novas entradas estão bloqueadas.',
      text: `Nada foi perdido: ${s.outbox_pending} ${s.outbox_pending === 1 ? 'evento aguarda' : 'eventos aguardam'} na fila. Os stops seguem ativos e o robô tenta de novo sozinho.`,
    };
  if (s.state === 'OFFLINE')
    return {
      tone: 'off',
      title: 'O robô está desligado ou sem sinal.',
      text: 'Os números abaixo são os últimos conhecidos. Ligue o robô pelo Controle.',
      action: toControl,
    };
  if (s.stops_blind)
    return {
      tone: 'warn',
      title: 'Sem preços de mercado no momento.',
      text: 'Novas entradas pausadas; seus stops continuam ativos. Se a falta de preços passar de 5 minutos com posição aberta, você será avisado.',
    };
  if (s.state === 'PAUSED') {
    const first = s.pause_reasons[0];
    return {
      tone: 'warn',
      title: 'O robô está pausado.',
      text: first
        ? `${first.reason} (${first.operator}, ${hhmm(first.at)}). Novas entradas estão bloqueadas; posições abertas seguem protegidas por stop.`
        : 'Novas entradas estão bloqueadas; posições abertas seguem protegidas por stop.',
      action: toControl,
    };
  }
  if (s.state === 'STOPPED' || s.state === 'STOPPING')
    return {
      tone: 'off',
      title: s.state === 'STOPPING' ? 'O robô está encerrando.' : 'O robô está parado.',
      text: 'Nenhuma nova operação será aberta. Posições abertas seguem protegidas por stop.',
      action: toControl,
    };
  if (s.state === 'STARTING')
    return {
      tone: 'off',
      title: 'O robô está iniciando.',
      text: 'Conferindo posições e dados antes de operar.',
    };
  if (page === 'controle')
    return {
      tone: 'ok',
      title: 'Operando normalmente, sem pausas nem travas ativas.',
      text: `${s.started_at ? `Ligado desde ${new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(s.started_at))}${s.run_id ? ` (run ${s.run_id})` : ''}. ` : ''}Posições abertas continuam protegidas por stop em qualquer parada.`,
    };
  return openCount > 0
    ? {
        tone: 'ok',
        title: `Tudo certo: ${openCount} ${openCount === 1 ? 'operação aberta, protegida' : 'operações abertas, protegidas'} por stop.`,
        text: 'O robô segue acompanhando a posição e só abre outra se passar nos filtros de risco.',
      }
    : {
        tone: 'ok',
        title: 'Tudo certo: o robô está ligado e esperando uma boa oportunidade.',
        text: 'Nenhuma posição aberta, 100% em USDT. Ficar parado em USDT é uma decisão válida: o robô só entra quando passa nos filtros de risco.',
      };
}

export function StateHero({ status, page }: { status: BotStatus; page: 'dashboard' | 'controle' }) {
  const calendar = useCalendar().data;
  const base = heroFor(status, page);
  const hero: HeroCopy =
    calendar?.state === 'expired' && base.tone === 'ok'
      ? {
          tone: 'warn',
          title: 'O robô está ligado, mas não abre novas operações.',
          text: 'O calendário de eventos está vencido. Posições abertas seguem protegidas por stop.',
          action: { to: '/calendario', label: 'Ver calendário' },
        }
      : base;
  const expiring = page === 'dashboard' && calendar?.state === 'expiring' && base.tone === 'ok';
  return (
    <>
      <Hero headingId={`hero-${page}`} tone={hero.tone} title={hero.title}>
        {hero.text}
        {hero.action ? (
          <>
            {' '}
            <Link to={hero.action.to} className="font-semibold underline underline-offset-2">
              {hero.action.label}
            </Link>
          </>
        ) : null}
      </Hero>
      {expiring ? (
        <p className="flex items-center gap-2 rounded-xl border border-warn bg-warn-bg px-3.5 py-2.5 text-sec">
          <TriangleAlert className="size-4 shrink-0 text-warn" aria-hidden="true" />
          <span>
            O calendário de eventos vence em breve.{' '}
            <Link to="/calendario" className="font-semibold underline underline-offset-2">
              Ver calendário
            </Link>
          </span>
        </p>
      ) : null}
    </>
  );
}
