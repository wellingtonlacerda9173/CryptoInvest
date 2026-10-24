import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { nextDelay, useReconnectingSocket, type SocketLike } from './useReconnectingSocket';

class Fake implements SocketLike {
  static all: Fake[] = [];
  onopen: SocketLike['onopen'] = null;
  onmessage: SocketLike['onmessage'] = null;
  onclose: SocketLike['onclose'] = null;
  onerror: SocketLike['onerror'] = null;
  closed = false;
  constructor() {
    Fake.all.push(this);
  }
  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  Fake.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe('nextDelay', () => {
  it('cresce exponencialmente, com teto de 30 s e jitter de 50 a 100%', () => {
    expect(nextDelay(0, () => 1)).toBe(1000);
    expect(nextDelay(3, () => 1)).toBe(8000);
    expect(nextDelay(3, () => 0)).toBe(4000);
    expect(nextDelay(20, () => 1)).toBe(30_000);
  });
});

describe('useReconnectingSocket', () => {
  const setup = (onMessage = vi.fn()) =>
    renderHook(() =>
      useReconnectingSocket({
        url: 'ws://x',
        factory: () => new Fake(),
        onMessage,
        backoff: (n) => 1000 * 2 ** n,
      }),
    );

  it('conecta, entrega mensagens JSON e marca como aberto', () => {
    const onMessage = vi.fn();
    const { result } = setup(onMessage);
    expect(result.current.state).toBe('connecting');
    act(() => Fake.all[0]!.onopen?.({}));
    expect(result.current.state).toBe('open');
    act(() => Fake.all[0]!.onmessage?.({ data: '{"type":"status"}' }));
    expect(onMessage).toHaveBeenCalledWith({ type: 'status' }, expect.any(Function));
    expect(result.current.lastMessageAt).not.toBeNull();
  });

  it('ao cair vira "reconnecting" e reconecta com espera crescente', () => {
    const { result } = setup();
    act(() => Fake.all[0]!.onopen?.({}));
    act(() => Fake.all[0]!.onclose?.({}));
    expect(result.current.state).toBe('reconnecting');
    expect(Fake.all).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(999));
    expect(Fake.all).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(1));
    expect(Fake.all).toHaveLength(2);

    act(() => Fake.all[1]!.onclose?.({}));
    act(() => void vi.advanceTimersByTime(1999));
    expect(Fake.all).toHaveLength(2);
    act(() => void vi.advanceTimersByTime(1));
    expect(Fake.all).toHaveLength(3);
  });

  it('ao reabrir volta ao normal e zera a espera', () => {
    const { result } = setup();
    act(() => Fake.all[0]!.onclose?.({}));
    act(() => void vi.advanceTimersByTime(1000));
    act(() => Fake.all[1]!.onopen?.({}));
    expect(result.current.state).toBe('open');
    act(() => Fake.all[1]!.onclose?.({}));
    act(() => void vi.advanceTimersByTime(1000));
    expect(Fake.all).toHaveLength(3);
  });

  it('ignora mensagens que nao sao JSON e fecha o socket ao desmontar', () => {
    const onMessage = vi.fn();
    const { unmount } = setup(onMessage);
    act(() => Fake.all[0]!.onmessage?.({ data: 'nao-json' }));
    expect(onMessage).not.toHaveBeenCalled();
    unmount();
    expect(Fake.all[0]!.closed).toBe(true);
    act(() => void vi.advanceTimersByTime(60_000));
    expect(Fake.all).toHaveLength(1);
  });

  it('watchdog: conexao aberta e muda (sem mensagem nem ping) vira reconnecting, fecha e reconecta', () => {
    const { result } = renderHook(() =>
      useReconnectingSocket({
        url: 'ws://x',
        factory: () => new Fake(),
        onMessage: vi.fn(),
        backoff: () => 1000,
        staleAfterMs: () => 50_000,
      }),
    );
    act(() => Fake.all[0]!.onopen?.({}));
    act(() => void vi.advanceTimersByTime(40_000));
    act(() => Fake.all[0]!.onmessage?.({ data: '{"type":"ping"}' }));
    act(() => void vi.advanceTimersByTime(45_000));
    expect(result.current.state).toBe('open');

    act(() => void vi.advanceTimersByTime(10_000));
    expect(result.current.state).toBe('reconnecting');
    expect(Fake.all[0]!.closed).toBe(true);
    expect(Fake.all).toHaveLength(1);
    act(() => void vi.advanceTimersByTime(1000));
    expect(Fake.all).toHaveLength(2);
    act(() => Fake.all[1]!.onopen?.({}));
    expect(result.current.state).toBe('open');
  });
});
