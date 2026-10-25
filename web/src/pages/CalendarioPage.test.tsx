import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { makeCalendar, type CalendarVariant } from '@/test/calendarData';
import { problem, renderPage, stubApi } from '@/test/render';
import CalendarioPage from './CalendarioPage';

const NOW = Date.parse('2026-11-03T15:00:00Z');

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'], now: NOW });
});
afterEach(() => vi.useRealTimers());

const show = (variant: CalendarVariant) => {
  stubApi({ 'GET /calendar': () => makeCalendar(variant, NOW) });
  return renderPage(<CalendarioPage />);
};

describe('Calendario', () => {
  it('ok: selo em dia, robo sem pausa e proximos eventos em lista', async () => {
    show('ok');
    const seal = await screen.findByRole('status');
    expect(seal).toHaveTextContent('Calendário em dia');
    expect(seal).toHaveTextContent('Calendário válido por mais 81 dias.');
    expect(screen.getByText('Sem evento por perto')).toBeInTheDocument();
    expect(
      screen.getByText(/Próxima pausa: Emprego nos EUA \(payroll\), em 3 dias\./),
    ).toBeInTheDocument();
    const items = within(screen.getByRole('list')).getAllByRole('listitem');
    expect(items).toHaveLength(5);
    expect(items[0]).toHaveTextContent('Emprego EUA');
    expect(items[0]).toHaveTextContent('Fonte: BLS (governo dos EUA)');
    expect(screen.getByText('Seu fuso').nextSibling).toHaveTextContent(/UTC/);
  });

  it('expirando: aviso com dias e ultima verificacao sem mudanca', async () => {
    show('expirando');
    expect(await screen.findByText('Atenção: calendário vence em 9 dias')).toBeInTheDocument();
    expect(screen.getByText(/Verificado hoje às/)).toBeInTheDocument();
    expect(screen.getByText('Sem mudanças: o calendário já estava em dia.')).toBeInTheDocument();
  });

  it('vencido: travado, parado por seguranca, sem eventos e tentativa falhou', async () => {
    show('vencido');
    expect(await screen.findByText('Calendário vencido')).toBeInTheDocument();
    expect(screen.getByText('O robô está parado agora')).toBeInTheDocument();
    expect(screen.getByText('Por segurança, até o calendário voltar.')).toBeInTheDocument();
    expect(screen.getByText('Nenhum evento para mostrar.')).toBeInTheDocument();
    expect(screen.getByText('Venceu há 3 dias')).toBeInTheDocument();
    expect(screen.getByText(/Tentativa hoje às .* falhou/)).toBeInTheDocument();
    expect(screen.getByText(/Última atualização bem-sucedida/)).toBeInTheDocument();
    expect(screen.queryByText(/event_calendar_stale/)).not.toBeInTheDocument();
  });

  it('janela ativa: selo de atencao (nunca verde), card parado agora e item em andamento', async () => {
    show('ativo');
    expect(
      await screen.findByText('Calendário em dia, com evento em andamento'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Calendário em dia')).not.toBeInTheDocument();
    expect(await screen.findByText('O robô está parado agora')).toBeInTheDocument();
    expect(
      screen.getByText(/Por causa de “Emprego nos EUA \(payroll\)”\. Volta a operar às/),
    ).toBeInTheDocument();
    const active = screen.getAllByRole('listitem').find((li) => li.dataset.active === 'true');
    expect(active).toHaveTextContent('Robô parado agora');
    expect(active).toHaveTextContent('Em andamento');
  });

  it('carregando: esqueleto com aria-busy e nenhum estado inventado', () => {
    show('ok');
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.queryByText(/vencido/)).not.toBeInTheDocument();
  });

  it('erro de carga: mensagem da spec, sem selo, e tenta de novo', async () => {
    stubApi({ 'GET /calendar': () => problem(500, 'Falha') });
    renderPage(<CalendarioPage />);
    expect(await screen.findByText(/Não consegui carregar o calendário\./)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Tentar de novo/ })).toBeInTheDocument();
    expect(screen.queryByText('Calendário em dia')).not.toBeInTheDocument();
  });
});
