import { spawn, type ChildProcess } from 'node:child_process';
import { existsSync, openSync, rmSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

export const STATE_FILE = resolve(import.meta.dirname, '.stack.json');
export const STOP_FILE = resolve(import.meta.dirname, '.stop');

let stack: ChildProcess | undefined;

/** Sobe a pilha real (banco descartavel + API + Redis + robo fake) e guarda as credenciais de teste. */
export default async function globalSetup() {
  rmSync(STOP_FILE, { force: true });
  rmSync(STATE_FILE, { force: true });
  const logFd = openSync(resolve(import.meta.dirname, '.stack.log'), 'w');
  stack = spawn(
    'uv',
    [
      'run',
      'python',
      '-m',
      'tests.e2e_stack',
      '--ready-file',
      STATE_FILE,
      '--stop-file',
      STOP_FILE,
      '--api-port',
      '8765',
    ],
    { cwd: resolve(import.meta.dirname, '../..'), stdio: ['ignore', logFd, logFd] },
  );
  const proc = stack;
  let exited: number | null = null;
  proc.on('exit', (code) => (exited = code ?? -1));
  for (let i = 0; i < 800 && !existsSync(STATE_FILE); i++) {
    if (exited !== null) throw new Error(`a pilha real encerrou (codigo ${exited})`);
    await new Promise((r) => setTimeout(r, 250));
  }
  if (!existsSync(STATE_FILE)) throw new Error('a pilha real nao ficou pronta em 200 s');
  await waitForApi();
  return async () => {
    writeFileSync(STOP_FILE, 'stop');
    const proc = stack!;
    await new Promise<void>((done) => {
      if (proc.exitCode !== null) return done();
      proc.on('exit', () => done());
      setTimeout(done, 30_000);
    });
    rmSync(STATE_FILE, { force: true });
    rmSync(STOP_FILE, { force: true });
  };
}

async function waitForApi() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch('http://127.0.0.1:8765/auth/me');
      if (r.status === 401) return;
    } catch {
      /* ainda subindo */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('a API real nao respondeu');
}
