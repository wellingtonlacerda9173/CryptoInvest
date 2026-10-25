import { describe, expect, it } from 'vitest';
import { BOT_STATES, DISCONNECTED_FACE, describeBotState } from './bot-state';
import { GLOSSARY } from './glossary';

describe('glossario', () => {
  it('tem os 15 termos do design system mais 4 das telas de mercado, 7 de backtest e configurações e 2 do calendário, todos com explicacao', () => {
    const entries = Object.values(GLOSSARY);
    expect(entries).toHaveLength(28);
    for (const e of entries) {
      expect(e.term.length).toBeGreaterThan(0);
      expect(e.text.length).toBeGreaterThan(20);
    }
  });
});

describe('estado do robo', () => {
  it('toda face tem palavra e motivo (nunca so cor)', () => {
    for (const state of BOT_STATES) {
      const face = describeBotState({ state, stopsBlind: false, reason: null });
      expect(face.label).not.toBe('');
      expect(face.reason).not.toBe('');
    }
  });

  it('mapeia tons: verde, ambar, vermelho, cinza', () => {
    const tone = (state: (typeof BOT_STATES)[number], stopsBlind = false) =>
      describeBotState({ state, stopsBlind, reason: null }).tone;
    expect(tone('RUNNING')).toBe('ok');
    expect(tone('PAUSED')).toBe('warn');
    expect(tone('RUNNING', true)).toBe('warn');
    expect(tone('HALTED')).toBe('bad');
    expect(tone('FAILED_PERSISTENCE')).toBe('bad');
    expect(tone('STOPPED')).toBe('off');
  });

  it('stops cegos nao esconde um desligamento', () => {
    expect(describeBotState({ state: 'HALTED', stopsBlind: true, reason: null }).tone).toBe('bad');
  });

  it('desconectado tem palavra propria', () => {
    expect(DISCONNECTED_FACE.label).toBe('SEM CONEXÃO COM O PAINEL');
  });
});
