import { describe, expect, it } from 'vitest';
import { formatNumber, formatPercent, formatSigned, spokenSigned } from './format';

describe('formatacao pt-BR', () => {
  it('usa ponto de milhar, virgula decimal e menos tipografico', () => {
    expect(formatNumber(10184.32)).toBe('10.184,32');
    expect(formatNumber(-30.56)).toBe('−30,56');
    expect(formatNumber(-0.001, 2)).toBe('0,00');
    expect(formatNumber(-0.006, 2)).toBe('−0,01');
  });

  it('sinaliza ganho, perda e zero', () => {
    expect(formatSigned(12.4)).toBe('+12,40');
    expect(formatSigned(-30.56)).toBe('−30,56');
    expect(formatSigned(0)).toBe('0,00');
    expect(formatSigned(-0.001)).toBe('0,00');
  });

  it('formata porcentagem', () => {
    expect(formatPercent(-0.3, 2, true)).toBe('−0,30%');
    expect(formatPercent(1.8, 1)).toBe('1,8%');
  });

  it('descreve valores para leitores de tela sem depender do simbolo', () => {
    expect(spokenSigned(-30.56)).toBe('menos 30,56');
    expect(spokenSigned(0.49)).toBe('mais 0,49');
  });
});
