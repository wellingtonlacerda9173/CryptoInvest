import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { makePositions } from '@/mocks/mockState';
import { makeCandles, SETTINGS } from '@/test/marketData';
import { problem, renderPage, stubApi } from '@/test/render';
import MercadoPage from './MercadoPage';

vi.mock('@/components/kit/CandleChart', () => ({
  CandleChart: (p: { markers?: unknown[]; levels?: { title: string }[]; label: string }) => (
    <div data-testid="grafico" role="img" aria-label={p.label}>
      {p.markers?.length} marcadores; {p.levels?.map((l) => l.title).join(',')}
    </div>
  ),
}));

describe('Mercado', () => {
  it('mostra o grafico, marcadores, stops/alvos e a tabela das ultimas velas', async () => {
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () => makeCandles(),
    });
    renderPage(<MercadoPage />);
    const chart = await screen.findByTestId('grafico');
    expect(chart).toHaveTextContent('1 marcadores; Stop,Entrada,Alvo');
    expect(screen.getAllByText('Barras elefante').length).toBeGreaterThan(0);
    const table = screen.getByRole('region', { name: 'Tabela das últimas velas' });
    expect(within(table).getAllByRole('row')).toHaveLength(21);
    expect(screen.getByText(/Esperou: sem sinal/)).toBeInTheDocument();
  });

  const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString();
  const decision = (id: number, tier: 'entered' | 'blocked' | 'quiet', text: string) => ({
    id,
    t: ago(30),
    tier,
    label: tier === 'entered' ? 'Entrou' : tier === 'blocked' ? 'Não entrou' : 'Esperou',
    text,
    code: 'x',
    strategy: null,
  });

  it('avisa com o horario quando o robo parou de gravar velas', async () => {
    const old = makeCandles().candles.map((k, i, all) => ({
      ...k,
      t: ago((all.length - i + 5) * 60),
    }));
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () => makeCandles({ candles: old }),
    });
    renderPage(<MercadoPage />);
    expect(
      await screen.findByText(
        /O gráfico está desatualizado: o robô não está gravando velas \(último dado às \d{2}:\d{2}\)/,
      ),
    ).toBeInTheDocument();
  });

  it('sem atraso nas velas, nao mostra o aviso de desatualizado', async () => {
    stubApi({ 'GET /settings': () => SETTINGS, 'GET /market/candles': () => makeCandles() });
    renderPage(<MercadoPage />);
    await screen.findByTestId('grafico');
    expect(screen.queryByText(/gráfico está desatualizado/)).not.toBeInTheDocument();
  });

  it('lista as ultimas decisoes em texto, com link para a tela Decisoes', async () => {
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () =>
        makeCandles({
          decisions: [
            decision(1, 'blocked', 'O calendário de eventos está vencido.'),
            decision(2, 'entered', 'Operação aberta.'),
          ],
        }),
    });
    renderPage(<MercadoPage />);
    const list = await screen.findByRole('heading', { name: 'Últimas decisões neste gráfico' });
    const section = list.closest('section') as HTMLElement;
    expect(
      within(section).getByText(/Não entrou: O calendário de eventos está vencido/),
    ).toBeInTheDocument();
    expect(within(section).getByText(/Entrou: Operação aberta/)).toBeInTheDocument();
    expect(within(section).getByRole('link', { name: /Ver todas as decisões/ })).toHaveAttribute(
      'href',
      '/decisoes?par=BTC%2FUSDT',
    );
  });

  it('"sem sinal" fica desligado e so e pedido a API quando o interruptor e ligado', async () => {
    const api = stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () =>
        makeCandles({ decisions: [decision(1, 'quiet', 'Nenhuma estratégia viu oportunidade.')] }),
    });
    renderPage(<MercadoPage />);
    await screen.findByTestId('grafico');
    const toggle = screen.getByRole('checkbox', { name: /Mostrar também "sem sinal"/ });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/Nenhuma decisão relevante neste período/)).toBeInTheDocument();
    expect(api.find('GET', '/market/candles').at(-1)?.search).not.toContain('quiet');
    await userEvent.click(toggle);
    await waitFor(() => {
      expect(api.find('GET', '/market/candles').at(-1)?.search).toContain('quiet=true');
    });
    expect(
      await screen.findByText(/Esperou: Nenhuma estratégia viu oportunidade/),
    ).toBeInTheDocument();
  });

  it('explica a legenda das decisoes sem depender so de cor', async () => {
    stubApi({ 'GET /settings': () => SETTINGS, 'GET /market/candles': () => makeCandles() });
    renderPage(<MercadoPage />);
    await screen.findByTestId('grafico');
    const item = (txt: string) =>
      screen.getByText((_, el) => el?.tagName === 'LI' && el.textContent === txt);
    expect(item('● Entrou')).toBeInTheDocument();
    expect(item('■ Viu uma oportunidade, mas não entrou')).toBeInTheDocument();
    expect(
      screen.getByText((_, el) => el?.tagName === 'LI' && /^Vela atual/.test(el.textContent ?? '')),
    ).toBeInTheDocument();
  });

  it('troca de par e timeframe e pede os dados certos', async () => {
    const api = stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () => makeCandles(),
    });
    renderPage(<MercadoPage />);
    await screen.findByTestId('grafico');
    await userEvent.selectOptions(screen.getByLabelText('Par'), 'ETH/USDT');
    await userEvent.selectOptions(screen.getByLabelText('Tempo de cada vela'), '4h');
    await waitFor(() => {
      const last = api.find('GET', '/market/candles').at(-1);
      expect(last?.search).toContain('symbol=ETH%2FUSDT');
      expect(last?.search).toContain('timeframe=4h');
    });
  });

  it('esconde stops e alvos quando desmarcado', async () => {
    stubApi({ 'GET /settings': () => SETTINGS, 'GET /market/candles': () => makeCandles() });
    renderPage(<MercadoPage />);
    await screen.findByTestId('grafico');
    await userEvent.click(screen.getByRole('checkbox', { name: 'Stops e alvos' }));
    expect(screen.getByTestId('grafico')).not.toHaveTextContent('Stop');
  });

  it('estado vazio quando o par nao tem velas', async () => {
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () => makeCandles({ candles: [], markers: [], levels: [] }),
    });
    renderPage(<MercadoPage />);
    expect(await screen.findByText('Ainda sem velas para este par')).toBeInTheDocument();
  });

  it('erro do feed: aviso com tentar de novo, sem derrubar a tela', async () => {
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () => problem(500, 'Falha'),
    });
    renderPage(<MercadoPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Não foi possível carregar');
    expect(screen.getByRole('button', { name: /Tentar de novo/ })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1, name: 'Mercado' })).toBeInTheDocument();
  });

  it('avisa quando o robo esta sem precos (stops cegos)', async () => {
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () => makeCandles({ blind_since: new Date().toISOString() }),
    });
    renderPage(<MercadoPage />);
    expect(await screen.findByText(/Sem preços desde/)).toBeInTheDocument();
  });

  it('mostra a posicao aberta do par; sem posicao, diz que ficar parado e valido', async () => {
    stubApi({ 'GET /settings': () => SETTINGS, 'GET /market/candles': () => makeCandles() });
    renderPage(<MercadoPage />);
    expect(await screen.findByText('Comprado')).toBeInTheDocument();
    stubApi({
      'GET /settings': () => SETTINGS,
      'GET /market/candles': () => makeCandles(),
      'GET /positions': () => [],
    });
    renderPage(<MercadoPage />);
    expect(await screen.findByText(/Ficar fora também é uma decisão válida/)).toBeInTheDocument();
    expect(makePositions().length).toBeGreaterThan(0);
  });
});
