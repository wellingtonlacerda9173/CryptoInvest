import type { SocketFactory, SocketLike } from '@/realtime/useReconnectingSocket';

const live = new Set<FakeSocket>();
let down = false;

const pushAll = () => {
  for (const s of live) s.push();
};

const frame = (payload: unknown) => ({ data: JSON.stringify(payload) });

class FakeSocket implements SocketLike {
  onopen: SocketLike['onopen'] = null;
  onmessage: SocketLike['onmessage'] = null;
  onclose: SocketLike['onclose'] = null;
  onerror: SocketLike['onerror'] = null;
  private timer: ReturnType<typeof setInterval> | undefined;
  private open = false;
  private seq = 0;

  constructor() {
    setTimeout(() => {
      if (down) {
        this.onerror?.({});
        this.onclose?.({});
        return;
      }
      this.open = true;
      live.add(this);
      this.onopen?.({});
      this.onmessage?.(frame({ type: 'hello', runner_connected: true, role: 'admin' }));
      this.timer = setInterval(
        () => this.onmessage?.(frame({ type: 'ping', at: new Date().toISOString() })),
        15_000,
      );
    }, 50);
  }

  /** Avisa que o estado mudou: o painel revalida as leituras. */
  push() {
    if (!this.open) return;
    this.seq += 1;
    this.onmessage?.(
      frame({
        type: 'event',
        id: `mock:${this.seq}`,
        channel: 'status',
        at: new Date().toISOString(),
        data: {},
      }),
    );
  }

  drop() {
    this.close();
    this.onclose?.({});
  }

  close() {
    this.open = false;
    live.delete(this);
    clearInterval(this.timer);
  }

  send() {
    // o canal e somente leitura
  }
}

export const fakeSocketFactory: SocketFactory = () => new FakeSocket();

/** Controles para testes manuais e E2E: derrubar/restaurar a conexao e empurrar um novo estado. */
export const mockControls = {
  dropConnection() {
    down = true;
    for (const s of [...live]) s.drop();
  },
  restoreConnection() {
    down = false;
  },
  push: pushAll,
};
