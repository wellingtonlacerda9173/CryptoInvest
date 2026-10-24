// Gera tipos TypeScript a partir do contrato OpenAPI da API (../api/openapi.json).
// Uso: npm run gen:api   (saida em src/api/generated/schema.d.ts, ignorada pelo git)
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const input = resolve(root, '../api/openapi.json');
const output = resolve(root, 'src/api/generated/schema.d.ts');

if (!existsSync(input)) {
  console.warn(`gen:api: ${input} ainda nao existe (a API gera o arquivo). Nada a fazer.`);
  process.exit(0);
}

mkdirSync(dirname(output), { recursive: true });
const bin = resolve(root, 'node_modules/openapi-typescript/bin/cli.js');
const result = spawnSync(process.execPath, [bin, input, '-o', output], { stdio: 'inherit' });
process.exit(result.status ?? 1);
