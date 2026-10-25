# CryptoInvest local em Docker

O sistema completo (banco, Redis, migração, API, robô de paper e painel) rodando em contêineres na sua máquina, com
**um comando**, para você abrir o painel no navegador.

> **Isto NÃO é produção.** O painel é servido em HTTP puro e só em `127.0.0.1`, os cookies não usam `Secure`, a API roda em
> modo `dev` e não há TLS, HSTS nem balanceador. O robô opera em **PAPER** (dinheiro fictício) e o **Live continua
> bloqueado**. Para produção use o Swarm: [docs/deploy.md](deploy.md).

## Pré-requisitos

- Docker Desktop (Windows/macOS) ou Docker Engine 25+ com o plugin Compose v2, em execução.
- ~4 GB de RAM livres e acesso à internet (a primeira subida constrói as imagens e baixa as bases; o robô usa os
  dados de mercado **públicos** da Binance, sem chave).
- Nada mais: não precisa de Python, Node nem chaves de API.

## 1. Subir

Windows (PowerShell, na raiz do repositório):

```powershell
.\deploy\local-up.ps1 up
```

Linux/macOS:

```bash
./deploy/local-up.sh up
```

Na primeira execução o script gera **todos os segredos** em `deploy/local-secrets/` (fora do Git; só o seu usuário acessa
a pasta; nunca sobrescreve os existentes), constrói as imagens, sobe tudo e espera os serviços ficarem saudáveis. No fim
mostra a URL do painel e o primeiro acesso. **Faça cópia de `deploy/local-secrets/`**: sem `api_master_keys` os 2FA
cadastrados ficam irrecuperáveis.

O que sobe (projeto Compose `cryptoinvest-local`, volumes e redes com esse prefixo):

| Serviço | Papel |
|---|---|
| `timescaledb`, `redis` | banco e canal assinado (senha por arquivo); **sem porta publicada** |
| `migrate` | one-shot: `alembic upgrade head`, papéis do banco e `check-grants`; `api` e `runner` só iniciam depois dele |
| `api` | FastAPI (rede interna) |
| `runner` | robô de paper, run `paper-local`, 1 réplica, parada ordenada (até 120 s) |
| `web` | painel + proxy, o **único** publicado: `127.0.0.1:8088` |

## 2. Abrir o painel e criar o administrador

1. Abra **http://localhost:8088** (use `localhost`; `127.0.0.1` é recusado de propósito).
2. Crie o administrador (a senha é pedida por prompt, nunca vai na linha de comando):

   ```powershell
   .\deploy\local-up.ps1 create-admin          # Linux: ./deploy/local-up.sh create-admin
   ```

   Anote o **TOKEN DE ATIVAÇÃO** impresso (aparece uma vez e vale 24 h).
3. No painel, entre com usuário e senha. Na primeira vez a tela pede o token de ativação e mostra um **QR code**: leia-o
   no app autenticador (Google Authenticator, Microsoft Authenticator, Aegis, 1Password...), digite o código de 6 dígitos
   e guarde os **códigos de recuperação** que aparecem (uma única vez).
4. Nos próximos acessos: usuário, senha e o código do app.

## 3. O que esperar

- **Robô parado ou sem sinais**: normal. Ele roda em paper e só decide quando fecha uma vela de 1 h.
- **Calendário de eventos fechado**: o calendário de volatilidade vem sem cobertura, então o robô **recusa toda entrada**
  (as decisões aparecem como puladas, com o motivo do calendário). É o comportamento seguro e é o padrão.
- **Estados vazios**: sem operações, sem posições e curva de patrimônio curta no começo.
- O Live aparece como indisponível e a trava mostra "sem teste" até haver diário elegível.

### Ver o robô operar (apenas teste)

```powershell
.\deploy\local-up.ps1 up -OpenCalendar        # Linux: ./deploy/local-up.sh up --open-calendar
```

Liga `--allow-open-calendar` no robô, num run separado (`paper-local-open`). **Aviso:** o robô passa a operar sem a
cobertura do calendário e esse run fica **inelegível para o Live para sempre**; nunca o use como evidência. Para voltar
ao normal suba sem a opção (o run `paper-local` continua intacto).

## 4. Dados de demonstração (opcional)

Para ver as telas preenchidas (operações, decisões, curva de patrimônio) sem esperar dias de paper:

```powershell
.\deploy\local-up.ps1 demo                    # exige o administrador já criado
.\deploy\local-up.ps1 demo -Remove            # Linux: demo --remove
```

Como é seguro: a demo vive num **banco separado** (`cryptoinvest_demo`, no mesmo servidor), nunca no diário real. O
usuário administrador é copiado para ele; a API passa a ler esse banco e o robô fica **parado** enquanto a demo estiver
ativa (entre de novo no painel: as sessões antigas não existem no banco demo). Os dados são fictícios e rotulados: run
`demo-paper`, versão de código `demo`, motivos das decisões começando por **"DEMO ·"**, e o run usa calendário aberto,
portanto é **inelegível para o Live** (a trava mostra "Inelegível"). Como trades e decisões são *append-only* por trigger,
a remoção correta é descartar o banco demo inteiro (`DROP DATABASE`): `demo -Remove` faz isso e reinicia API e robô no
diário real. Nada do diário real é apagado nem misturado. Se você criar usuários durante a demo, eles valem só nela.

## 5. Backtest, validação e lições (perfil `tools`)

Sob demanda, sem publicar portas, com a mesma imagem, segredos e rede. Relatórios vão para o volume `reports` e o cache
de velas para o volume `cache` (compartilhado com a API):

```powershell
.\deploy\local-up.ps1 tool backtest --symbol BTC/USDT --years 1
.\deploy\local-up.ps1 tool validate --end 2026-10-01 --years 2
.\deploy\local-up.ps1 tool lessons --no-ai
.\deploy\local-up.ps1 tool admin unlock-user --username fulano
# equivalente puro: docker compose run --rm backtest --help
```

Listar os relatórios gerados (volume `cryptoinvest-local_reports`):

```bash
docker run --rm -v cryptoinvest-local_reports:/r:ro --entrypoint ls cryptoinvest/api:local -R /r
```

### Analista de IA (opcional, desligado)

Crie `deploy/local-secrets/anthropic_api_key` com a sua chave (nunca no Git) e troque a API pela variante com a chave:
`docker compose -p cryptoinvest-local stop api` seguido de `docker compose -p cryptoinvest-local --profile ai up -d ai-api`.

## 6. Dia a dia

| Quero | Windows | Linux |
|---|---|---|
| Ver logs (todos ou um serviço) | `.\deploy\local-up.ps1 logs runner` | `./deploy/local-up.sh logs runner` |
| Ver o estado | `.\deploy\local-up.ps1 status` | `./deploy/local-up.sh status` |
| Parar (**preserva** os dados) | `.\deploy\local-up.ps1 down` | `./deploy/local-up.sh down` |
| Backup (banco + diário do robô, em `backups/`) | `.\deploy\local-up.ps1 backup` | `./deploy/local-up.sh backup` |
| Apagar tudo do projeto | `.\deploy\local-up.ps1 reset` | `./deploy/local-up.sh reset` |

`reset` apaga **apenas** os volumes e redes do projeto `cryptoinvest-local` (pede para você digitar o nome do projeto) e
mantém `deploy/local-secrets/`. Não mexe em nenhum outro contêiner, volume ou rede, inclusive o ambiente de
desenvolvimento (`docker-compose.dev.yml`, projeto `cryptoinvest-dev`).

### Trocar a porta

```powershell
$env:CRYPTOINVEST_WEB_PORT = "9090"; .\deploy\local-up.ps1 up        # Linux: CRYPTOINVEST_WEB_PORT=9090 ./deploy/local-up.sh up
```

Use a mesma variável nos comandos seguintes (a API só aceita a origem do painel na porta configurada). Se a porta estiver
ocupada, o `up` falha com essa dica.

## 7. Diferenças para produção (docs/deploy.md)

| | Local (`docker-compose.yml`) | Produção (`deploy/stack.yml`, Swarm) |
|---|---|---|
| Orquestração | Compose, 1 máquina | Docker Swarm, secrets externos, `placement` |
| Acesso | HTTP, `127.0.0.1:8088` | HTTPS (balanceador ou `web-tls`), domínio |
| Cookies / API | sem `Secure`, `API_ENVIRONMENT=dev`, sem HSTS | `Secure`, `prod`, HSTS, proxies confiáveis |
| `TRUSTED_LB_CIDR` | `127.0.0.1/32` (nenhum balanceador: o `X-Forwarded-For` do cliente é ignorado) | CIDR real do balanceador, obrigatório |
| Segredos | arquivos em `deploy/local-secrets/` (mesmos nomes + `live_authorization_hmac_key`) | `docker secret` |
| Redes | `backend`/`edge` internas, `egress`, `frontend` | overlays cifradas, sub-rede fixa |
| Robô | run `paper-local` | run `paper-main` |

Mesmos itens do stack de produção: imagens com digest, usuários não root, `read_only`, `cap_drop: ALL`,
`no-new-privileges`, healthchecks e a ordem migração, banco/Redis e depois API/robô. Não é uma validação de HTTPS, de
Swarm nem de um balanceador: isso só se verifica no ambiente de produção.
