import { describe, expect, it } from 'vitest';
import { DISCONNECTED_FACE, NO_SIGNAL_FACE, resolveTopBarFace } from '@/lib/bot-state';
import { defaultSocketUrl } from './ConnectionProvider';

describe('defaultSocketUrl', () => {
  const loc = { protocol: 'https:', host: 'app.exemplo.com' };
  it('base relativa usa o host da pagina', () => {
    expect(defaultSocketUrl('/api', loc)).toMatch(
      /^wss:\/\/app\.exemplo\.com\/api\/ws\?channels=status,/,
    );
  });
  it('base absoluta usa o host dela, nao o da pagina', () => {
    expect(defaultSocketUrl('https://api.exemplo.com/v1/', loc)).toMatch(
      /^wss:\/\/api\.exemplo\.com\/v1\/ws\?channels=/,
    );
    expect(defaultSocketUrl('http://127.0.0.1:8000/api', loc)).toMatch(
      /^ws:\/\/127\.0\.0\.1:8000\/api\/ws/,
    );
  });
});

describe('resolveTopBarFace', () => {
  const loading = { tone: 'off', label: 'c', reason: 'c' } as const;
  const failed = { tone: 'offline', label: 'f', reason: 'f' } as const;
  const base = {
    disconnected: false,
    runnerConnected: true as boolean | null,
    status: { state: 'RUNNING', stopsBlind: false, reason: null } as const,
    loadFailed: false,
    loading,
    failed,
  };
  it('robo sem sinal (texto, nao so cor) quando o WS avisa runnerConnected=false', () => {
    expect(resolveTopBarFace({ ...base, runnerConnected: false })).toBe(NO_SIGNAL_FACE);
    expect(NO_SIGNAL_FACE.label).toBe('ROBÔ SEM SINAL');
  });
  it('painel desconectado tem prioridade; null (desconhecido) nao e sem sinal', () => {
    expect(resolveTopBarFace({ ...base, disconnected: true, runnerConnected: false })).toBe(
      DISCONNECTED_FACE,
    );
    expect(resolveTopBarFace({ ...base, runnerConnected: null }).label).toBe('Operando');
  });
});
