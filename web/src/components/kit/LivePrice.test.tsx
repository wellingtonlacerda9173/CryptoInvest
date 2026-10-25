import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { problem, renderPage, stubApi } from '@/test/render';
import { LivePrice } from './LivePrice';

const quote = (symbol: string, price: number, stale = false) => ({
  symbol,
  price,
  change_24h_pct: -1.25,
  as_of: '2026-01-01T00:00:00Z',
  age_seconds: stale ? 40 : 1,
  stale,
});

const ui = (
  <LivePrice
    symbol="BTC/USDT"
    symbols={['BTC/USDT', 'ETH/USDT']}
    fallbackPrice={50_000}
    fallbackChangePct={2}
  />
);

describe('LivePrice', () => {
  it('mostra o preco ao vivo, a variacao de 24 h e o outro par', async () => {
    stubApi({
      'GET /market/ticker': () => ({
        quotes: [quote('BTC/USDT', 64_321.5), quote('ETH/USDT', 3_100.25)],
      }),
    });
    renderPage(ui);
    expect(await screen.findByText('64.321,50')).toBeInTheDocument();
    expect(screen.getAllByText('Ao vivo').length).toBe(1);
    expect(screen.getByText(/−1,25% em 24 h/)).toBeInTheDocument();
    expect(screen.getByText('3.100,25')).toBeInTheDocument();
    expect(screen.getByRole('status', { name: '' })).toHaveTextContent('Cotação ao vivo');
  });

  it('cotacao velha vem marcada como atrasada', async () => {
    stubApi({ 'GET /market/ticker': () => ({ quotes: [quote('BTC/USDT', 64_000, true)] }) });
    renderPage(ui);
    expect(await screen.findByText('Atrasado')).toBeInTheDocument();
    expect(screen.getByText('64.000,00')).toBeInTheDocument();
  });

  it('falha da API: avisa que nao ha cotacao ao vivo e usa o preco do grafico', async () => {
    stubApi({ 'GET /market/ticker': () => problem(502, 'Falha') });
    renderPage(ui);
    expect(await screen.findAllByText('Sem cotação ao vivo')).not.toHaveLength(0);
    expect(screen.getByText('50.000,00')).toBeInTheDocument();
    expect(screen.getByText(/último preço do gráfico/)).toBeInTheDocument();
  });

  it('pede o par escolhido primeiro e so pares validos', async () => {
    const api = stubApi({ 'GET /market/ticker': () => ({ quotes: [quote('ETH/USDT', 3_000)] }) });
    renderPage(
      <LivePrice
        symbol="ETH/USDT"
        symbols={['BTC/USDT', 'lixo!', 'ETH/USDT']}
        fallbackPrice={1}
        fallbackChangePct={0}
      />,
    );
    await screen.findByText('3.000,00');
    expect(api.find('GET', '/market/ticker')[0]?.search).toBe('?symbols=ETH%2FUSDT%2CBTC%2FUSDT');
  });
});
