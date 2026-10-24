import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { TooltipProvider } from '@/components/ui/tooltip';
import { GLOSSARY } from '@/lib/glossary';
import { EmptyState } from './EmptyState';
import { GlossaryTerm } from './GlossaryTerm';
import { KpiCard } from './KpiCard';
import { RiskIncreaseWarning } from './RiskIncreaseWarning';
import { SimulatedTag } from './SimulatedTag';
import { StatusIndicator } from './StatusIndicator';

describe('StatusIndicator', () => {
  it('tem role=status, palavra e motivo (nunca so cor)', () => {
    render(
      <StatusIndicator tone="warn" label="Pausado" reason="perda diária atingida até 00:00 UTC" />,
    );
    const status = screen.getByRole('status');
    expect(status).toHaveAttribute('aria-live', 'polite');
    expect(status).toHaveTextContent('Pausado');
    expect(status).toHaveTextContent('perda diária atingida até 00:00 UTC');
  });

  it('so ok e warn pulsam; vermelho e cinza ficam fixos', () => {
    const dot = (tone: 'ok' | 'warn' | 'bad' | 'off' | 'offline') => {
      const { container } = render(<StatusIndicator tone={tone} label="x" reason="y" />);
      return container.querySelector('span[aria-hidden]')!.className;
    };
    expect(dot('ok')).toContain('status-pulse-ok');
    expect(dot('warn')).toContain('status-pulse-warn');
    expect(dot('bad')).not.toContain('pulse');
    expect(dot('off')).not.toContain('pulse');
  });
});

describe('KpiCard', () => {
  it('mostra seta junto da cor no delta', () => {
    render(
      <KpiCard label="Resultado do dia" value="−30,56" unit="USDT" tone="loss" delta="−0,30%" />,
    );
    expect(screen.getByText('▼')).toBeInTheDocument();
    expect(screen.getByText('−0,30%')).toBeInTheDocument();
  });

  it('carregando mostra esqueleto e marca aria-busy', () => {
    const { container } = render(<KpiCard label="x" loading />);
    expect(container.firstElementChild).toHaveAttribute('aria-busy', 'true');
    expect(container.querySelectorAll('.skeleton').length).toBeGreaterThan(0);
  });
});

describe('outros componentes', () => {
  it('SimulatedTag e EmptyState', () => {
    render(
      <>
        <SimulatedTag />
        <EmptyState title="Nenhuma operação ainda">Ficar parado é normal.</EmptyState>
      </>,
    );
    expect(screen.getByText('SIMULADO')).toBeInTheDocument();
    expect(screen.getByText('Nenhuma operação ainda')).toBeInTheDocument();
  });

  it('RiskIncreaseWarning traz antes, agora e custo por perda', () => {
    render(<RiskIncreaseWarning before="0,5%" after="0,9%" costBefore="50" costAfter="90" />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Isto aumenta o risco.');
    expect(alert).toHaveTextContent('Antes: 0,5%. Agora: 0,9%.');
    expect(alert).toHaveTextContent('Cada perda custará até 90 USDT (hoje: 50)');
  });

  it('GlossaryTerm abre com foco (teclado) e mostra o texto do glossario', async () => {
    const user = userEvent.setup();
    render(
      <TooltipProvider>
        <p>
          O <GlossaryTerm term="drawdown" /> está em 1,8%.
        </p>
      </TooltipProvider>,
    );
    await user.tab();
    expect(await screen.findByRole('tooltip')).toHaveTextContent(GLOSSARY.drawdown.text);
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('GlossaryTerm abre ao toque/clique', async () => {
    const user = userEvent.setup();
    render(<GlossaryTerm term="oco" />);
    await user.click(screen.getByRole('button', { name: 'OCO' }));
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Ordem dupla de proteção');
  });
});
