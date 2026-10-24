import { describe, expect, it } from 'vitest';
import {
  buildRequest,
  DEFAULT_WIZARD,
  firstInvalidStep,
  stateFromParams,
  validateAll,
  validateStep,
  type WizardState,
} from './wizardModel';

const TODAY = '2026-10-24';
const with_ = (over: Partial<WizardState>): WizardState => ({ ...DEFAULT_WIZARD, ...over });

describe('validacao do assistente de backtest', () => {
  it('o padrao e valido', () => {
    expect(validateAll(DEFAULT_WIZARD, TODAY)).toEqual({});
    expect(firstInvalidStep(DEFAULT_WIZARD, TODAY)).toBe(-1);
  });

  it('pares: de 1 a 4, no formato MOEDA/MOEDA', () => {
    expect(validateStep(0, with_({ symbols: [] }), TODAY).symbols).toMatch(/pelo menos um/);
    expect(
      validateStep(0, with_({ symbols: ['A/USDT', 'B/USDT', 'C/USDT', 'D/USDT', 'E/USDT'] }), TODAY)
        .symbols,
    ).toMatch(/No máximo 4/);
    expect(validateStep(0, with_({ symbols: ['btcusdt'] }), TODAY).symbols).toMatch(/formato/);
  });

  it('periodo: nao antes de 17/08/2017, fim depois do inicio e nunca no futuro', () => {
    const custom = (start: string, end: string) => with_({ preset: 'custom', start, end });
    expect(validateStep(1, custom('2017-08-16', '2018-01-01'), TODAY).start).toMatch(
      /17\/08\/2017/,
    );
    expect(validateStep(1, custom('2017-08-17', '2018-01-01'), TODAY)).toEqual({});
    expect(validateStep(1, custom('2025-01-02', '2025-01-01'), TODAY).end).toMatch(
      /depois da inicial/,
    );
    expect(validateStep(1, custom('2026-01-01', '2026-10-25'), TODAY).end).toMatch(/futuro/);
    expect(validateStep(1, custom('2026-01-01', '2026-01-02'), TODAY).period).toMatch(/2 dias/);
    expect(validateStep(1, custom('', ''), TODAY).start).toBeDefined();
  });

  it('estrategias: ao menos uma', () => {
    expect(validateStep(2, with_({ strategies: [] }), TODAY).strategies).toBeDefined();
  });

  it('parametros e premissas respeitam as faixas da API', () => {
    const wrong = validateStep(
      3,
      with_({
        minConfidence: '1.5',
        rsiMin: '100',
        rewardRisk: '1',
        capital: '0',
        takerFeePct: '2',
        slippageBps: '501',
        latency: '0',
      }),
      TODAY,
    );
    expect(Object.keys(wrong).sort()).toEqual(
      [
        'capital',
        'latency',
        'minConfidence',
        'rewardRisk',
        'rsiMin',
        'slippageBps',
        'takerFeePct',
      ].sort(),
    );
    expect(validateStep(3, with_({ minConfidence: '0,6', rewardRisk: '2.5' }), TODAY)).toEqual({});
    expect(validateStep(3, with_({ latency: '1.5' }), TODAY).latency).toMatch(/inteiro/);
  });

  it('walk-forward: periodo precisa caber treino + teste', () => {
    const wf = with_({ walkForward: true, preset: '0.25', trainDays: '180', testDays: '60' });
    expect(validateStep(3, wf, TODAY).period).toMatch(/240 dias/);
    expect(validateStep(3, { ...wf, preset: '1' }, TODAY)).toEqual({});
    expect(validateStep(3, { ...wf, preset: '1', trainDays: '3' }, TODAY).trainDays).toBeDefined();
  });
});

describe('pedido de backtest', () => {
  it('monta o corpo da API: anos, taxas em fracao e so os parametros preenchidos', () => {
    const body = buildRequest(with_({ minConfidence: '0,6', takerFeePct: '0.10', preset: '2' }));
    expect(body).toMatchObject({
      symbols: ['BTC/USDT'],
      timeframe: '1h',
      years: 2,
      strategies: ['elephant', 'ema'],
      params: { min_confidence: 0.6 },
      capital: 10000,
      taker_fee: 0.001,
      latency_candles: 1,
      walk_forward: false,
    });
    expect(body).not.toHaveProperty('grid');
    expect(body).not.toHaveProperty('start');
  });

  it('periodo escolhido vira start/end em UTC e a grade so vai com walk-forward', () => {
    const body = buildRequest(
      with_({
        preset: 'custom',
        start: '2026-01-01',
        end: '2026-02-01',
        walkForward: true,
        grid: ' a=1,2 ',
      }),
    );
    expect(body).toMatchObject({
      start: '2026-01-01T00:00:00Z',
      end: '2026-02-01T00:00:00Z',
      grid: 'a=1,2',
    });
    expect(body).not.toHaveProperty('years');
  });

  it('refazer: params do job voltam para o formulario', () => {
    const s = stateFromParams({
      symbols: ['ETH/USDT'],
      timeframe: '4h',
      start: '2025-01-01T00:00:00+00:00',
      end: '2025-06-01T00:00:00+00:00',
      strategies: ['ema'],
      params: { rsi_min: 45 },
      capital: '5000',
      taker_fee: '0.001',
      maker_fee: '0.00075',
      slippage_bps: '5',
      latency_candles: 2,
      walk_forward: true,
      train_days: 90,
      test_days: 30,
    });
    expect(s).toMatchObject({
      symbols: ['ETH/USDT'],
      preset: 'custom',
      start: '2025-01-01',
      end: '2025-06-01',
      rsiMin: '45',
      takerFeePct: '0.1',
      makerFeePct: '0.075',
      walkForward: true,
      trainDays: '90',
    });
  });
});
