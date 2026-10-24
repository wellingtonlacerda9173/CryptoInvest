import { useState, type ReactNode } from 'react';
import { ConfirmDialog } from '@/components/kit/ConfirmDialog';
import { EmptyState } from '@/components/kit/EmptyState';
import { GlossaryTerm } from '@/components/kit/GlossaryTerm';
import { KpiCard } from '@/components/kit/KpiCard';
import { LimitBar } from '@/components/kit/LimitBar';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { RiskIncreaseWarning } from '@/components/kit/RiskIncreaseWarning';
import { SimulatedTag } from '@/components/kit/SimulatedTag';
import { StatusIndicator } from '@/components/kit/StatusIndicator';
import { notify } from '@/components/kit/Toaster';
import { ConnectionBanner } from '@/components/layout/ConnectionBanner';
import { PageHeader } from '@/components/layout/PageHeader';
import { TopBarView, type TopBarViewProps } from '@/components/layout/TopBarView';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { Tooltip } from '@/components/ui/tooltip';
import { DISCONNECTED_FACE, describeBotState } from '@/lib/bot-state';
import { GLOSSARY, type GlossaryKey } from '@/lib/glossary';

const paper = { mode: 'paper', balance: 10184.32, dayPnl: -30.56, dayPnlPct: -0.3 } as const;

const BARS: { caption: string; frame?: boolean; props: TopBarViewProps }[] = [
  {
    caption: 'RUNNING - Paper',
    props: {
      ...paper,
      face: describeBotState({ state: 'RUNNING', stopsBlind: false, reason: null }),
    },
  },
  {
    caption: 'PAUSED - perda diária',
    props: {
      ...paper,
      dayPnl: -101.2,
      dayPnlPct: -1,
      face: describeBotState({
        state: 'PAUSED',
        stopsBlind: false,
        reason: 'perda diária atingida até 00:00 UTC',
      }),
    },
  },
  {
    caption: 'HALTED - drawdown de 5%',
    props: {
      ...paper,
      dayPnl: -512.7,
      dayPnlPct: -5,
      face: describeBotState({ state: 'HALTED', stopsBlind: false, reason: null }),
    },
  },
  {
    caption: 'FAILED_PERSISTENCE - banco indisponível',
    props: {
      ...paper,
      face: describeBotState({ state: 'FAILED_PERSISTENCE', stopsBlind: false, reason: null }),
    },
  },
  {
    caption: 'Stops cegos - sem dados de mercado',
    props: {
      ...paper,
      face: describeBotState({ state: 'RUNNING', stopsBlind: true, reason: null }),
    },
  },
  {
    caption: 'STOPPED - parada ordenada',
    props: {
      ...paper,
      face: describeBotState({ state: 'STOPPED', stopsBlind: false, reason: null }),
    },
  },
  {
    caption: 'Sem conexão com o painel',
    props: { ...paper, stale: true, face: DISCONNECTED_FACE },
  },
  {
    caption: 'Backtest (revisão de histórico, nunca real)',
    props: {
      mode: 'backtest',
      balance: 10000,
      dayPnl: 842.1,
      dayPnlPct: 8.42,
      face: { tone: 'off', label: 'Parado', reason: 'resultado de simulação passada' },
    },
  },
  {
    caption: 'LIVE em operação (moldura vermelha + selo cheio)',
    frame: true,
    props: {
      mode: 'live',
      balance: 2540.18,
      dayPnl: 12.4,
      dayPnlPct: 0.49,
      face: describeBotState({ state: 'RUNNING', stopsBlind: false, reason: null }),
    },
  },
];

const SWATCHES = [
  'bg',
  'surface',
  'surface-2',
  'border',
  'border-strong',
  'text',
  'muted',
  'gain',
  'gain-bg',
  'loss',
  'loss-bg',
  'warn',
  'warn-bg',
  'info',
  'info-bg',
  'neutral',
  'backtest',
  'backtest-bg',
  'live-fill',
  'primary',
  'focus',
] as const;

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-3">
      <h2 id={id} className="text-[12px] font-bold uppercase tracking-[0.06em] text-muted">
        {title}
      </h2>
      {children}
    </section>
  );
}

function Samples() {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap gap-2.5">
        <ModeBadge mode="backtest" />
        <ModeBadge mode="paper" />
        <ModeBadge mode="live" />
      </div>
      <div className="flex flex-wrap gap-2.5">
        <StatusIndicator tone="ok" label="Operando" reason="aguardando sinal" />
        <StatusIndicator tone="warn" label="Pausado" reason="perda diária" />
      </div>
      <KpiCard label="Resultado do dia" value="−30,56" unit="USDT" tone="loss" delta="−0,30%" />
      <LimitBar
        name="Drawdown"
        label="Drawdown"
        valueText="4,6% de 5%"
        value={4.6}
        limit={5}
        note="se chegar a 5%, o robô desliga"
      />
      <p>
        Texto com <GlossaryTerm term="drawdown" /> e <SimulatedTag />.
      </p>
    </div>
  );
}

export default function KitPage() {
  const [dialog, setDialog] = useState<'neutral' | 'danger' | null>(null);
  const [risk, setRisk] = useState('0,9');
  const [on, setOn] = useState(true);
  const [lastData] = useState(() => Date.now() - 14_000);

  return (
    <>
      <PageHeader
        title="Kit de componentes"
        description="Todos os componentes em todos os estados. Página interna, só no ambiente de desenvolvimento."
      />

      <Section id="k-bars" title="Barra superior por estado">
        <div className="flex flex-col gap-3.5">
          {BARS.map(({ caption, frame, props }) => (
            <div key={caption}>
              <div className="mb-1.5 text-cap font-bold uppercase tracking-[0.06em] text-muted">
                {caption}
              </div>
              <div
                className={
                  frame
                    ? 'overflow-hidden rounded-card border-[3px] border-live-fill'
                    : 'overflow-hidden rounded-[14px] border border-border bg-surface'
                }
              >
                <TopBarView {...props} canStop as="div" />
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section id="k-conn" title="Faixa de desconexão">
        <div className="overflow-hidden rounded-xl border border-border">
          <ConnectionBanner lastDataAt={lastData} />
        </div>
      </Section>

      <Section id="k-modes" title="Selos de modo e status">
        <Card>
          <div className="flex flex-wrap gap-2.5">
            <ModeBadge mode="backtest" />
            <ModeBadge mode="paper" />
            <ModeBadge mode="live" />
          </div>
          <p className="text-sec text-muted">
            Três sinais redundantes: cor, forma da borda (pontilhada, tracejada, sólida) e palavra.
            Em LIVE, toda a janela ganha moldura vermelha.
          </p>
          <div className="flex flex-wrap gap-2.5">
            <StatusIndicator tone="ok" label="Operando" reason="aguardando sinal" />
            <StatusIndicator tone="warn" label="Pausado" reason="perda diária" />
            <StatusIndicator
              tone="bad"
              label="DESLIGADO por drawdown"
              reason="só você pode religar"
            />
            <StatusIndicator tone="off" label="Parado" reason="sem novas entradas" />
            <StatusIndicator {...DISCONNECTED_FACE} tone="offline" />
          </div>
          <p>
            Valor do Paper sempre marcado: Patrimônio <SimulatedTag />
          </p>
        </Card>
      </Section>

      <Section id="k-kpi" title="Cartões de indicador">
        <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
          <KpiCard
            label="Patrimônio (simulado)"
            value="10.184,32"
            unit="USDT"
            tone="gain"
            delta="+1,84% desde o início"
          />
          <KpiCard
            label="Resultado do dia"
            value="−30,56"
            unit="USDT"
            tone="loss"
            delta="−0,30% (1 operação encerrada)"
          />
          <KpiCard
            label={<GlossaryTerm term="drawdown">Drawdown atual</GlossaryTerm>}
            value="4,6%"
            tone="warn"
            delta="perto do limite"
            glyph={false}
          />
          <KpiCard
            label="Operações abertas"
            value="1"
            unit="de 2"
            tone="info"
            delta="BTC/USDT +0,6 R"
            glyph={false}
          />
          <KpiCard label="Carregando" loading />
        </div>
      </Section>

      <Section id="k-limits" title="Barras de limite">
        <Card>
          <div className="flex flex-col gap-4">
            <LimitBar
              name="Perda diária"
              label="Perda diária"
              valueText="0,3% de 1%"
              value={0.3}
              limit={1}
              note="se chegar a 1%, o robô para de abrir operações até 00:00 UTC"
            />
            <LimitBar
              name="Perdas seguidas (ETH/USDT)"
              label="Perdas seguidas"
              valueText="1 de 2"
              value={1}
              limit={2}
              level="warn"
              stateText="Atenção: 1 perda a menos do limite"
              note="com 2 seguidas o par pausa por 24 h"
            />
            <LimitBar
              name="Drawdown"
              label="Drawdown"
              valueText="4,8% de 5%"
              value={4.8}
              limit={5}
              note="se chegar a 5%, o robô desliga e só você religa"
            />
          </div>
        </Card>
      </Section>

      <Section id="k-form" title="Formulário, aumento de risco e erro">
        <Card>
          <Field
            label="Risco por operação (% do saldo)"
            htmlFor="kit-r1"
            help="Padrão 0,5%. Máximo permitido 1%."
            helpId="kit-r1h"
          >
            <Input
              id="kit-r1"
              value={risk}
              onChange={(e) => setRisk(e.target.value)}
              inputMode="decimal"
              aria-describedby="kit-r1h"
            />
          </Field>
          <RiskIncreaseWarning before="0,5%" after="0,9%" costBefore="50" costAfter="90" />
          <Field
            label="Perda máxima por dia (%)"
            htmlFor="kit-r2"
            error="O máximo permitido é 5%. Digite 5 ou menos."
            errorId="kit-r2e"
          >
            <Input id="kit-r2" defaultValue="7" aria-invalid="true" aria-describedby="kit-r2e" />
          </Field>
          <div className="flex flex-wrap gap-2.5">
            <Button variant="primary">Salvar alterações</Button>
            <Button>Descartar</Button>
            <Button disabled>Nada para salvar</Button>
          </div>
          <label className="flex min-h-11 items-center gap-3 font-semibold">
            <Switch checked={on} onCheckedChange={setOn} aria-label="Robô ligado" />
            Robô {on ? 'ligado' : 'desligado'}
          </label>
        </Card>
      </Section>

      <Section id="k-feedback" title="Avisos, esqueleto e estado vazio">
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Toasts</CardTitle>
            </CardHeader>
            <div className="flex flex-wrap gap-2.5">
              <Button onClick={() => notify.success('Salvo', 'Risco por operação agora é 0,9%.')}>
                Sucesso
              </Button>
              <Button
                onClick={() =>
                  notify.warn(
                    'Pausado: dados de mercado parados',
                    'Tentando reconectar. Seus stops seguem ativos.',
                  )
                }
              >
                Atenção
              </Button>
              <Button
                onClick={() =>
                  notify.error(
                    'Não foi possível gravar',
                    'Nada foi perdido. Tentaremos de novo sozinhos.',
                  )
                }
              >
                Erro
              </Button>
            </div>
            <div className="flex flex-col gap-2.5">
              <Skeleton className="h-[18px] w-3/5" />
              <Skeleton className="h-12" />
            </div>
          </Card>
          <Card>
            <EmptyState
              title="Nenhuma operação ainda"
              action={<Button>Ver decisões ao vivo</Button>}
            >
              O robô só opera quando surge uma boa oportunidade. Ficar parado é normal.
            </EmptyState>
          </Card>
        </div>
      </Section>

      <Section id="k-dialogs" title="Diálogos e dicas">
        <Card>
          <div className="flex flex-wrap items-center gap-2.5">
            <Button onClick={() => setDialog('neutral')}>Abrir confirmação</Button>
            <Button variant="danger" onClick={() => setDialog('danger')}>
              Abrir confirmação de risco
            </Button>
            <Tooltip content="Dica curta para um ícone ou item recolhido." side="bottom">
              <Button>Passe o mouse ou foque</Button>
            </Tooltip>
          </div>
          <ConfirmDialog
            open={dialog === 'neutral'}
            onOpenChange={(o) => !o && setDialog(null)}
            title="Salvar mudança?"
            description="Nada acontece até você confirmar."
            confirmLabel="Salvar"
            onConfirm={() => {
              notify.success('Salvo');
            }}
          />
          <ConfirmDialog
            open={dialog === 'danger'}
            onOpenChange={(o) => !o && setDialog(null)}
            tone="danger"
            title="Reativar o robô?"
            description="Você revisou o que aconteceu?"
            confirmLabel="Confirmar: reativar"
            onConfirm={() => {
              notify.success('Robô reativado');
            }}
          />
        </Card>
      </Section>

      <Section id="k-glossary" title="Glossário (15 termos)">
        <Card>
          <ul className="m-0 grid list-none grid-cols-2 gap-x-4 gap-y-2 p-0 sm:grid-cols-3 lg:grid-cols-5">
            {(Object.keys(GLOSSARY) as GlossaryKey[]).map((k) => (
              <li key={k}>
                <GlossaryTerm term={k} />
              </li>
            ))}
          </ul>
        </Card>
      </Section>

      <Section id="k-themes" title="Os dois temas lado a lado">
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="dark rounded-card border border-border-strong bg-bg p-4 text-text">
            <h3 className="mb-3 font-bold">Escuro</h3>
            <Samples />
          </div>
          <div className="light rounded-card border border-border-strong bg-bg p-4 text-text">
            <h3 className="mb-3 font-bold">Claro</h3>
            <Samples />
          </div>
        </div>
      </Section>

      <Section id="k-tokens" title="Tokens de cor">
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-2.5">
          {SWATCHES.map((t) => (
            <div
              key={t}
              className="overflow-hidden rounded-xl border border-border bg-surface text-cap"
            >
              <i className="block h-11" style={{ background: `var(--${t})` }} />
              <span className="block px-2.5 py-1.5 text-muted">
                <b className="block text-text">{t}</b>
                {`--${t}`}
              </span>
            </div>
          ))}
        </div>
      </Section>
    </>
  );
}
