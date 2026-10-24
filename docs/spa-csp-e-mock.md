# SPA: CSP do host e modo mock

## CSP do host da SPA

O `index.html` tem um script inline (anti-flash do tema, lê `localStorage["ci-theme"]` antes do React) e um
`<style>` inline com o fundo inicial. Com `script-src 'self'` o navegador bloqueia o script: ele precisa de hash
(ou nonce, se o host reescreve o HTML a cada resposta).

Hashes do `web/index.html` atual (SHA-256 do conteúdo exato entre as tags, espaços incluídos):

| Trecho | Hash |
| --- | --- |
| `<script>` anti-flash | `'sha256-6g9Yr4Z5uQLOKYFWnN3z0UaPj+qslZDOMIpfzLFIiaU='` |
| `<style>` de fundo (do `dist/index.html`: o Vite minifica o CSS inline) | `'sha256-8kvkwELjYxqgmcCymBgnwPIUNqW7QGz4P6+5470rI0g='` |

Qualquer edição (inclusive reformatar com Prettier) muda o hash. O teste `src/lib/csp.test.ts` falha quando o
hash documentado deixa de bater com o `index.html`; recalcule assim:

```bash
node -e "const c=require('crypto'),h=require('fs').readFileSync('dist/index.html','utf8');for(const m of h.matchAll(/<(script|style)(?![^>]*src=)[^>]*>([\s\S]*?)<\/>/g))console.log(m[1],'sha256-'+c.createHash('sha256').update(m[2]).digest('base64'))"
```

Exemplo de cabeçalho (SPA servida em `app.exemplo.com`, API em `/api` no mesmo host):

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'sha256-6g9Yr4Z5uQLOKYFWnN3z0UaPj+qslZDOMIpfzLFIiaU='; style-src 'self' 'sha256-8kvkwELjYxqgmcCymBgnwPIUNqW7QGz4P6+5470rI0g='; style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self' wss://app.exemplo.com; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'
```

- `connect-src`: inclua o `wss://` do mesmo host (o painel abre `/api/ws`). Com `VITE_API_BASE` absoluto, liste o
  host da API (`https://api.exemplo.com wss://api.exemplo.com`).
- `style-src-attr 'unsafe-inline'` cobre o `style="color-scheme: dark"` do `<html>` e os estilos inline dos
  componentes Radix; scripts continuam sem `unsafe-inline`.
- `script.style.colorScheme = ...` (CSSOM) não é bloqueado pela CSP.
- Não use `'unsafe-inline'` em `script-src`.

## Modo mock

`VITE_MOCK=1` (`npm run dev:mock`) liga o MSW, login e WebSocket falsos com credenciais fictícias. É só para
desenvolvimento e demonstração: nunca vai para produção.

- O build de produção (`npm run build`, sem `VITE_MOCK`) não inclui `src/mocks/` e remove o worker do MSW do `dist/`.
- Nunca defina `VITE_MOCK` no pipeline/ambiente de build de produção.
- Confira antes de publicar: `dist/` não pode conter `mockServiceWorker.js` nem a palavra `__ciMock`.
