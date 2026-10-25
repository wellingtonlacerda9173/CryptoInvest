// Falha a build se: o dist tem o modo mock, ou um script/style inline nao tem o hash no CSP do nginx.
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const [dist, headersFile] = process.argv.slice(2);
const html = readFileSync(join(dist, 'index.html'), 'utf8');
const csp = readFileSync(headersFile, 'utf8');
const errors = [];

for (const m of html.matchAll(/<(script|style)(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/\1>/g)) {
  const hash = `'sha256-${createHash('sha256').update(m[2]).digest('base64')}'`;
  if (!csp.includes(hash)) errors.push(`hash ausente no CSP para <${m[1]}> inline: ${hash}`);
}

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path);
    else {
      if (name === 'mockServiceWorker.js') errors.push(`mock no dist: ${path}`);
      else if (/\.(js|html)$/.test(name) && readFileSync(path, 'utf8').includes('__ciMock')) errors.push(`__ciMock em ${path}`);
    }
  }
}
walk(dist);
if (!existsSync(join(dist, 'index.html'))) errors.push('dist/index.html ausente');
if (errors.length) {
  console.error(errors.join('\n'));
  process.exit(1);
}
console.log('dist ok: CSP confere e nao ha mock');
