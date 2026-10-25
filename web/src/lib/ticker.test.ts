import { describe, expect, it } from 'vitest';
import { resolveLiveState, tickerSymbols } from './ticker';

const q = (age_seconds: number, stale = false) => ({ age_seconds, stale });

describe('resolveLiveState', () => {
  it('sem cotacao ou sem leitura recebida: nenhuma', () => {
    expect(resolveLiveState(undefined, 1000, 2000)).toBe('none');
    expect(resolveLiveState(q(0), 0, 2000)).toBe('none');
  });
  it('recente: ao vivo; passou de 15 s (servidor ou tempo desde a leitura): atrasado', () => {
    expect(resolveLiveState(q(1), 10_000, 12_000)).toBe('live');
    expect(resolveLiveState(q(16, true), 10_000, 10_000)).toBe('delayed');
    expect(resolveLiveState(q(5), 10_000, 10_000 + 11_000)).toBe('delayed');
  });
  it('muito velha some em vez de mostrar preco antigo', () => {
    expect(resolveLiveState(q(5), 10_000, 10_000 + 120_000)).toBe('none');
  });
});

describe('tickerSymbols', () => {
  it('manda o par escolhido primeiro, descarta o que a API recusaria e limita a 8', () => {
    const many = Array.from({ length: 12 }, (_, i) => `P${i}/USDT`);
    const list = tickerSymbols(['BTC/USDT', 'inválido!', ...many], 'P11/USDT');
    expect(list[0]).toBe('P11/USDT');
    expect(list).toHaveLength(8);
    expect(list).not.toContain('inválido!');
    expect(new Set(list).size).toBe(8);
  });
  it('escolhido fora do padrao e ignorado', () => {
    expect(tickerSymbols(['ETH/USDT'], 'x')).toEqual(['ETH/USDT']);
  });
});
