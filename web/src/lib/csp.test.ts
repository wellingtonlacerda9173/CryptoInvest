import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const root = resolve(import.meta.dirname, '../..');
const sha = (body: string) => `sha256-${createHash('sha256').update(body).digest('base64')}`;

describe('CSP documentada', () => {
  const html = readFileSync(resolve(root, 'index.html'), 'utf8');
  const readme = readFileSync(resolve(root, 'README.md'), 'utf8');
  const doc = readFileSync(resolve(root, '../docs/spa-csp-e-mock.md'), 'utf8');

  // O hash do <style> vale para o dist/index.html (o Vite minifica o CSS inline), por isso so o script e conferido aqui.
  it('o hash do script inline do index.html bate com o documentado', () => {
    const hash = sha(/<script>([\s\S]*?)<\/script>/.exec(html)![1]!);
    expect(readme).toContain(hash);
    expect(doc).toContain(hash);
  });
});
