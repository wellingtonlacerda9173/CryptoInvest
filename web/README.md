# web

Painel web do robô de trading (CryptoInvest). Esqueleto da Fase 4: layout, design system, autenticação, tempo real e testes. Telas prontas: login/2FA, onboarding, Dashboard, Controle, Mercado, Decisões ao vivo, Operações, Desempenho, Backtest (assistente, progresso, relatório) e Configurações (abas, versões, chaves).

Design aprovado: `docs/design/` (tokens, componentes e wireframes). Tema escuro é o padrão, com alternância em um clique.

## Stack (versões fixas, sem `^` nem `~`, `.npmrc` com `save-exact=true`)

Vite 8.3.4 · React 19.3.1 · TypeScript 5.9.3 (strict + `noUncheckedIndexedAccess`) · Tailwind CSS 4.3.3 · shadcn/ui (componentes copiados em `src/components/ui`, sobre `radix-ui` 1.8.1) · TanStack Query 5.104.5 · React Router 7.18.4 · Lightweight Charts 5.2.1 · Recharts 3.10.1 · Zod 4.6.7 · sonner 2.0.8 · lucide-react 1.60.0 · Inter variável auto-hospedada (`@fontsource-variable/inter` 5.3.0, sem CDN) · ESLint 10.13.0 + Prettier 3.9.10 · Vitest 5.0.5 + Testing Library · Playwright 1.64.1 + `@axe-core/playwright` 4.14.0 · MSW 3.0.3 · openapi-typescript 7.13.0.

TypeScript fica na 5.9.3 (e não na 7.x) porque `openapi-typescript` 7.13 exige `typescript@^5`.

## Scripts

| Script                 | O que faz                                                                                                                     |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `npm run dev`          | Servidor de desenvolvimento. Proxy `/api` para `http://127.0.0.1:8000` (REST e WebSocket).                                    |
| `npm run dev:mock`     | Igual, com `VITE_MOCK=1`: dados fictícios via MSW, login e WebSocket falsos. Sem backend.                                     |
| `npm run build`        | `tsc` + build de produção em `dist/` (sem MSW, sem `/_kit`, sem dados fictícios).                                             |
| `npm run preview`      | Serve o build.                                                                                                                |
| `npm run lint`         | ESLint.                                                                                                                       |
| `npm run format:check` | Prettier (`npm run format` corrige).                                                                                          |
| `npm run typecheck`    | `tsc --noEmit`.                                                                                                               |
| `npm test`             | Vitest (componentes, tema, guarda de autenticação, HTTP, WebSocket).                                                          |
| `npm run test:e2e`     | Playwright: smoke, tema, Parar tudo, responsividade 1440/820/390, axe (2 temas) e capturas.                                   |
| `npm run gen:api`      | Gera `src/api/generated/schema.d.ts` a partir de `../api/openapi.json`. Se o arquivo ainda não existir, avisa e sai sem erro. |

E2E real (`npm run test:e2e:real`): sobe banco `*_test` descartavel + API + Redis + robo de mentira, com dados de exemplo das telas Mercado, Decisoes, Operacoes e Desempenho (`tests/e2e_stack.py`). O trace do Playwright fica desligado (montar o trace de ~10 testes com capturas de pagina inteira leva minutos ao fechar o contexto); ligue com `PW_TRACE=1`.

E2E usa o Edge instalado (`channel: 'msedge'`) para não baixar navegadores; ajuste com `PW_CHANNEL=chrome` ou rode `npx playwright install chromium` e `PW_CHANNEL=chromium`. As capturas vão para `docs/design/app-screenshots/`.

## Estrutura

```
src/
  styles/index.css      tokens (escuro/claro), tema Tailwind, animações, moldura do modo
  theme/                ThemeProvider, persistência em localStorage ("ci-theme")
  lib/                  formatação pt-BR, glossário (fonte única dos 15 termos), estados do robô
  api/                  cliente HTTP (credentials, RFC 7807, CSRF), schemas Zod, queries
  auth/                 camada abstrata AuthBackend, httpAuthBackend, AuthProvider, RequireAuth
  realtime/             useReconnectingSocket (backoff exponencial + jitter), ConnectionProvider
  components/ui/        primitivos shadcn (Button, Card, Dialog, Tooltip, Switch, ...)
  components/kit/       ModeBadge, StatusIndicator, KpiCard, LimitBar, ConfirmDialog, StopAll, ...
  components/layout/    TopBar, SideNav, BottomNav, ConnectionBanner, AppLayout
  pages/                rotas (stubs com skeleton/estado vazio) e /_kit (só em dev)
  mocks/                MSW + WebSocket falso (só com VITE_MOCK=1)
```

## Tema e anti-flash

`index.html` nasce com `class="dark"` e um script inline lê `localStorage["ci-theme"]` antes do React (e do CSS) aplicar a classe `dark` e `color-scheme`. Sem escolha salva, vale o escuro. Com CSP no host, o script precisa de hash: `'sha256-6g9Yr4Z5uQLOKYFWnN3z0UaPj+qslZDOMIpfzLFIiaU='` (o `<style>` inline, `'sha256-8kvkwELjYxqgmcCymBgnwPIUNqW7QGz4P6+5470rI0g='`). Exemplo de cabeçalho completo e como recalcular em [`docs/spa-csp-e-mock.md`](../docs/spa-csp-e-mock.md); o teste `src/lib/csp.test.ts` falha se o `index.html` mudar sem atualizar o hash.

## Autenticação e CSRF

- Sessão por cookie `httpOnly` emitido pela API; o JavaScript nunca o lê. Nenhum token ou chave fica em `localStorage` ou no bundle.
- `src/auth/types.ts` define `AuthBackend`; `httpAuthBackend` fala com `/auth/login`, `/auth/2fa/verify`, `/auth/me`, `/auth/logout` da API.
- Mutações enviam `X-CSRF-Token`: lido do cookie legível `__Host-ci_csrf`/`ci_csrf` (double-submit) ou do token devolvido no login e em `/auth/me`.
- `RequireAuth` guarda as rotas: carregando mostra skeleton, anônimo vai a `/login` (guardando a origem).
- Papéis: `admin` e `reader` (exibido como "Leitor"). Leitor vê o botão Parar tudo desabilitado com explicação.

## Parar tudo

`StopAllButton` (barra superior, sempre visível, igual no desktop e no celular) abre `AlertDialog` com a opção segura padrão. O comando é injetável: `<StopAllProvider stopAll={fn}>`. O padrão chama `POST /control/emergency-stop` e o aviso segue o estado do `Command` devolvido (aguardando entrega, entregue, aplicado...), nunca afirma venda sem confirmação do robô; falha 5xx/timeout/sem resposta diz "Não confirmado — confira o Histórico de controle" (`src/lib/commandFailure.ts`).

## Mocks (`VITE_MOCK=1`)

**O modo mock nunca vai para produção**: nunca defina `VITE_MOCK` no build de produção e confira que `dist/` não tem `mockServiceWorker.js` (ver [`docs/spa-csp-e-mock.md`](../docs/spa-csp-e-mock.md)).

Credenciais e dados fictícios ficam só em `src/mocks/` (o build normal não os inclui; o worker do MSW é removido do `dist/`). No console do navegador: `window.__ciMock.dropConnection()` / `restoreConnection()` / `setStatus({ mode: 'live' })` / `reset()` para exercitar desconexão e estados.
