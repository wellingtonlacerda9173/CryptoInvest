# Implantação (Docker Swarm, modo PAPER)

Runbook do operador. O stack roda o robô em **modo PAPER**. O **Live continua bloqueado** (`POST /live/activate` responde
409 e nenhum gateway real existe): ele é a fase **7B** e só depois dela o stack ganhará chaves da Binance. Nada deste
documento habilita ordens reais.

## Visão geral

| Peça | Imagem / arquivo | Papel |
|---|---|---|
| `timescaledb` | `timescale/timescaledb:2.30.2-pg17` (digest fixo) | banco, só na rede `backend` |
| `redis` | `redis:8.10.2-alpine` (digest fixo) | canal assinado API/runner, `requirepass` por secret, AOF |
| `migrate` | `Dockerfile` alvo `migrate` | job one-shot: alembic, `db_roles.sql`, `check-grants` |
| `api` | `Dockerfile` alvo `api` | FastAPI, 1 processo, `--ws-max-size 8192` |
| `runner` | `Dockerfile` alvo `runner` | robô de paper (`--run-id` estável, 1 réplica) |
| `web` | `Dockerfile.web` alvos `web` / `web-tls` | painel estático + proxy `/api` e `/api/ws`, CSP com hashes |

Redes: `backend` (interna: banco, Redis, migrate, api, runner), `edge` (interna: proxy ↔ api, sub-rede fixa
`10.77.1.0/24` = `API_TRUSTED_PROXIES`), `egress` (só saída para Binance/Anthropic: runner e api), `frontend` (só o proxy).

## 1. Pré-requisitos

- Docker Engine 25+ no nó, porta 443 (ou a do balanceador) liberada no firewall; Swarm não precisa de mais de um nó.
- Domínio apontando para o nó (`PANEL_DOMAIN`), certificado TLS (seção 12).
- Um registro de imagens (ou o próprio nó: com um nó só, as imagens locais bastam).
- O calendário de eventos de volatilidade (`config/volatility_events.json`, vai na imagem) **precisa declarar cobertura**
  (`loaded_at`/`valid_until`). Sem isso o runner recusa toda entrada (falha fechada, log `event_calendar_without_coverage`).
  Atualize o arquivo e reconstrua a imagem; só para paper e por decisão sua, `--allow-open-calendar` no `command` do runner.

## 2. Swarm e nó

```bash
docker swarm init --advertise-addr <IP-DO-NO>
docker node update --label-add cryptoinvest=primary <NOME-DO-NO>
```

Todos os serviços com estado (banco, Redis, runner, api, web) têm `placement` no nó rotulado: os volumes nomeados são
locais a ele (`timescale-data`, `redis-data`, `paper-data`, `api-cache`).

## 3. Secrets

```bash
./deploy/secrets.sh            # Windows: .\deploy\secrets.ps1
```

Cria `cryptoinvest_<nome>` (valor aleatório por stdin; nunca sobrescreve, nunca imprime): `postgres_password`,
`migrator_password`, `app_password`, `api_database_password`, `redis_password`, `api_session_secret`,
`api_recovery_hmac_key`, `command_hmac_key`, `api_master_keys`. **Faça cofre/backup de todos fora do servidor**: sem
`api_master_keys` os TOTP e as chaves da Binance cifradas ficam irrecuperáveis. Docker secrets não podem ser
alterados: rotação = criar `..._v2`, trocar no stack e remover o antigo (seção 10).

Mapeamento para o arquivo que o código lê em `/run/secrets` (`SECRETS_DIR` padrão):

| Secret Docker | Arquivo | Quem |
|---|---|---|
| `app_password` | `database_password` | runner, api (papel `cryptoinvest_app`) |
| `api_database_password` | `api_database_password` | api (papel `cryptoinvest_api`) |
| `command_hmac_key` | `command_hmac_key` | api e runner (mesma chave: assina comandos e eventos) |
| `redis_password` | `redis_password` | redis, api, runner |
| `api_session_secret`, `api_master_keys`, `api_recovery_hmac_key` | idem | só api |
| `postgres_password`, `migrator_password`, `app_password`, `api_database_password` | idem | só `migrate` (e o Postgres lê `postgres_password`) |

Opcionais, **não** definidos por padrão: `anthropic_api_key` (`deploy/stack.ai.yml`) e as chaves da Binance
(`binance_api_key`, `binance_api_secret`), que ficam **vazias/ausentes no paper** e só serão usadas na 7B.

```bash
printf '%s' "$CHAVE" | docker secret create cryptoinvest_anthropic_api_key -    # só se for ligar a IA
```

## 4. Build e push das imagens

```bash
export CRYPTOINVEST_TAG=0.1.0 REGISTRY=registro.exemplo.com/cryptoinvest   # sem REGISTRY, imagens locais "cryptoinvest/*"
for t in runner api migrate; do docker build --target $t -t $REGISTRY/$t:$CRYPTOINVEST_TAG .; done
docker build -f Dockerfile.web --target web -t $REGISTRY/web:$CRYPTOINVEST_TAG .          # HTTP atrás de balanceador TLS
docker build -f Dockerfile.web --target web-tls -t $REGISTRY/web-tls:$CRYPTOINVEST_TAG .  # HTTPS no próprio proxy
docker push $REGISTRY/runner:$CRYPTOINVEST_TAG   # idem api, migrate e a variante web escolhida
```

**Antes de implantar, varra as imagens em busca de CVEs** (`trivy image $REGISTRY/api:$CRYPTOINVEST_TAG`, idem runner,
migrate e web, ou `docker scout cves ...`) e não implante com achados críticos/altos sem decisão registrada. A
imagem `migrate` fixa o cliente do Postgres (`ARG PG_CLIENT_VERSION`, pacote Debian versionado): o Debian remove versões
antigas do espelho de segurança, então, se a build falhar com "Version not found", consulte
`apt-cache madison postgresql-client-15` e atualize o ARG.

Use sempre uma tag de versão (nunca `latest`; é o que permite o rollback). A build do painel falha se o `dist` tiver o
modo mock ou se o CSP do nginx deixar de ter o hash dos trechos inline (`docs/spa-csp-e-mock.md`): ao editar
`web/index.html`, recalcule os hashes e atualize `deploy/web/snippets/headers.conf`.

## 5. Deploy

```bash
export CRYPTOINVEST_TAG=0.1.0 PANEL_DOMAIN=painel.exemplo.com     # REGISTRY se aplicável
export TRUSTED_LB_CIDR=10.0.0.5/32      # só com stack.http.yml: IP/CIDR do balanceador (seção 12); sem ele o web não sobe
docker stack config -c deploy/stack.yml -c deploy/stack.http.yml > /dev/null    # validação local
docker stack deploy --with-registry-auth -c deploy/stack.yml -c deploy/stack.http.yml cryptoinvest
```

Escolha **um** override de publicação: `stack.http.yml` (porta 8080 em HTTP, só atrás de terminador TLS) ou
`stack.tls.yml` (80 → 443 e HTTPS no proxy, exige os secrets `cryptoinvest_tls_cert` e `cryptoinvest_tls_key`);
`stack.ai.yml` é opcional e combinável. As portas usam `mode: host` para preservar o IP real do cliente (o ingress do
Swarm faz SNAT e o throttle por IP passaria a enxergar um único cliente).

`docker stack deploy` não tem `depends_on`: `api` e `runner` reiniciam (política `on-failure`) até o `migrate` terminar.

## 6. Primeira migração e admin

O job `migrate` sobe junto e executa, nesta ordem: espera o banco; `alembic upgrade head`; `scripts/db_roles.sql`
(cria/atualiza os papéis `cryptoinvest_migrator|app|api` e os grants); `python -m api.cli check-grants`. As senhas vêm só
dos secrets (arquivo `.pgpass` em tmpfs; nunca argv ou log). Na primeira vez o alembic roda como o superusuário inicial;
depois, como `cryptoinvest_migrator` (dono das tabelas).

```bash
docker service logs cryptoinvest_migrate          # termina com {"event":"migrate","step":"done"}
docker service ps cryptoinvest_migrate            # Complete
```

**Reaplicar `db_roles` após cada migração:** o job já faz isso toda vez. Para rodar de novo sem mudar a versão:
`docker service update --force cryptoinvest_migrate`.

Criar o administrador (senha pedida no terminal; 2FA configurado no primeiro login do painel):

```bash
docker exec -it $(docker ps -q -f label=com.docker.swarm.service.name=cryptoinvest_api) \
  python -m api.cli create-admin --username ana.silva
```

O CLI imprime o token de ativação do 2FA: entregue-o ao usuário por canal seguro.

## 7. Atualização com rollback

`api` e `runner` **recusam subir** se o `alembic_version` do banco divergir do head que o código espera (a versão chega
pela função `public.schema_version()`, criada pelo `db_roles.sql`; mensagem "schema do banco (...) diverge do esperado").
`--skip-schema-check` (runner e `api.serve`) existe só para desenvolvimento e é recusado em produção.

**Release SEM migração** (rolling normal):

```bash
export CRYPTOINVEST_TAG=0.1.1
docker stack deploy --with-registry-auth -c deploy/stack.yml -c deploy/stack.http.yml cryptoinvest
docker stack ps cryptoinvest --no-trunc
```

**Release COM migração** (api e runner fora do ar durante a migração, nunca duas versões de schema em uso):

```bash
export CRYPTOINVEST_TAG=0.1.1
docker service scale cryptoinvest_runner=0 cryptoinvest_api=0
docker service ps cryptoinvest_runner cryptoinvest_api    # espere o desligamento ordenado (stop_grace_period: runner 120 s)
./deploy/backup.sh                                          # recomendado: backup antes de migrar
docker stack deploy --with-registry-auth -c deploy/stack.yml -c deploy/stack.http.yml cryptoinvest   # nova imagem do migrate
docker service update --force cryptoinvest_migrate          # roda a migração (migrate)
docker service logs cryptoinvest_migrate                    # termina com {"event":"migrate","step":"done"}
docker exec $(docker ps -q -f label=com.docker.swarm.service.name=cryptoinvest_timescaledb) \
  psql -U cryptoinvest -d cryptoinvest -tAc "SELECT version_num FROM alembic_version"    # confere o head do release
docker service scale cryptoinvest_api=1 cryptoinvest_runner=1                              # sobe a versão nova
```

O `check-grants` faz parte do job `migrate` (última etapa, falha o job se faltar permissão): confira `done` nos logs. A
consulta ao `alembic_version` acima equivale ao `alembic current`. Se `docker stack deploy` subir api/runner antes da
migração, eles recusam e reiniciam até o schema bater.

`runner`, `api`, banco e proxy atualizam com `order: stop-first` (nunca dois runners nem duas APIs), `parallelism: 1`
e `failure_action: rollback` (a janela `monitor` espera o healthcheck). Rollback manual:
`docker service rollback cryptoinvest_runner` (ou volte `CRYPTOINVEST_TAG` e reaplique). Migrações não voltam sozinhas:
só faça rollback de imagem para uma versão compatível com o schema atual.

O runner recebe `SIGTERM` e tem `stop_grace_period: 120s` para encerrar ordenadamente: posições abertas mantêm os stops
no estado salvo (`paper_stopped`); o próximo start com o mesmo `--run-id` (`paper-main`) retoma estado, risco e halt.

## 8. Backup e restauração

```bash
./deploy/backup.sh            # Windows: .\deploy\backup.ps1  (BACKUP_DIR=./backups, RETENTION_DAYS=14)
./deploy/restore.sh verify ./backups/20261025T010000Z     # restaura num Timescale descartável e confere
```

O backup gera `db.dump` (pg_dump custom comprimido, sem donos/grants), `paper-data.tar.gz` (volume do runner, com
outbox/comandos), `alembic_version.txt`, `counts.txt` (contagem por tabela) e `SHA256SUMS`. O `verify` valida o checksum,
restaura num container **sem rede**, compara `alembic current` e as contagens, e remove o container. Agende o backup
(cron/Agendador de Tarefas) e **rode o `verify` periodicamente**; guarde cópias fora do nó. Contagens divergem se o
backup foi feito com o sistema ativo e a tabela cresceu entre a contagem e o dump (`--allow-drift` aceita; o banco é a
fonte da verdade, `data/paper` é cópia "melhor esforço" de arquivos em uso).

**O backup contém dados sensíveis**: hashes de senha, segredos TOTP e credenciais da Binance cifradas (inúteis sem
`api_master_keys`, que fica no cofre). `backup.sh` usa `umask 077`; `backup.ps1`/`restore.ps1` restringem a ACL da pasta
ao usuário atual (`icacls`). Trate cópias externas com o mesmo cuidado (cifre-as).

**Backup de `paper-data` é "melhor esforço"**: com o runner ativo os arquivos mudam durante a cópia. Para um backup
consistente pare o runner antes (`docker service scale cryptoinvest_runner=0`, espere o desligamento ordenado) e religue depois.

Restauração real do banco (substitui o banco atual por um restaurado e VALIDADO):

```bash
docker service scale cryptoinvest_api=0 cryptoinvest_runner=0       # e espere ficarem 0/0 (docker service ls)
./deploy/restore.sh apply ./backups/<dir> --container <id do timescaledb> --confirm-database cryptoinvest
# Windows: .\deploy\restore.ps1 apply .\backups\<dir> -Container <id> -ConfirmDatabase cryptoinvest
```

O `apply`, em ordem: (1) confere `SHA256SUMS` e `pg_restore --list` do dump dentro do contêiner; (2) exige que você
DIGITE `--confirm-database <nome>` (sem valor padrão aceito) e que `api`/`runner` estejam com 0 réplicas (ou
`--i-stopped-services`/`-IStoppedServices`, p.ex. fora do Swarm); (3) grava um dump de SEGURANÇA do banco atual
(`./backups/pre-restore-<ts>`, com SHA256; `SAFETY_DIR` muda o local); (4) restaura num banco temporário
`<db>_restore_<ts>` com o `pg_restore` estrito (qualquer erro aborta) e valida `alembic_version` e as contagens contra
o manifesto (`--allow-drift` aceita contagens diferentes se as tabelas conferem); (5) só então encerra conexões e troca
por renomeação: o banco antigo vira `<db>_old_<ts>` e o novo assume `<db>`. Qualquer divergência encerra com código
!= 0, o banco original intacto e o temporário removido; o dump de segurança permanece.

Depois: `docker service update --force cryptoinvest_migrate` (reatribui donos e grants), suba `api`/`runner`
(`scale ...=1`) e confira o painel. **Para reverter** (api/runner parados):
`ALTER DATABASE cryptoinvest RENAME TO cryptoinvest_failed_<ts>; ALTER DATABASE cryptoinvest_old_<ts> RENAME TO cryptoinvest;`.
O banco antigo ocupa disco até você descartá-lo, **manualmente e só depois de confirmar** o resultado:
`DROP DATABASE cryptoinvest_old_<ts>`.

Restauração do volume `paper-data` (runner parado; o contêiner descartável não tem rede):

```bash
docker run --rm --network none --user 0 -v cryptoinvest_paper-data:/data -v "$PWD/backups/<dir>:/backup:ro" \
  --entrypoint sh timescale/timescaledb:2.30.2-pg17 -c 'cd /data && tar xzf /backup/paper-data.tar.gz && chown -R 10001:10001 /data'
```

O `chown` devolve os arquivos ao usuário 10001 da aplicação.

## 9. Operação diária

- Painel: `https://<PANEL_DOMAIN>`. Saúde: `docker service ls`, `docker stack ps cryptoinvest`; `GET /api/ready`.
- Logs JSON estruturados (`docker service logs -f cryptoinvest_runner|api|web`); configure um driver de log com rotação
  (`/etc/docker/daemon.json`: `"log-opts": {"max-size": "20m", "max-file": "5"}`) ou envie ao seu coletor.
- Healthchecks: api (`GET /health`), runner (`status.json` do run regravado há menos de 150 s), web (`/healthz`),
  Postgres (`pg_isready`), Redis (`PING` autenticado).
- Alertas externos opcionais (Telegram/e-mail): variáveis `NOTIFY_*` e secrets `telegram_bot_token`/`smtp_password`.

## 10. Rotação de chaves

- **Chave mestra** (`api_master_keys`, cifra TOTP e credenciais): dentro do container da api,
  `python -m api.cli new-master-key` grava no arquivo, que é um secret somente leitura. Procedimento no Swarm: gere uma
  nova versão (`1:...` mais `2:...`) num arquivo temporário seguro, crie `cryptoinvest_api_master_keys_v2`, aponte
  `source` desse secret em `stack.yml` (ou use `SECRET_PREFIX`), reimplante a api e rode
  `docker exec <api> python -m api.cli reencrypt`. Só remova a versão antiga quando `reencrypt` terminar.
- **`command_hmac_key`** (api e runner): crie o secret novo e reimplante **api e runner juntos**
  (`docker stack deploy`); comandos em voo assinados com a chave antiga são descartados (`bad_signature`); a parada de
  emergência é reenviada pela api com a chave nova. Pare comandos pendentes antes se houver posição aberta.
- **`api_recovery_hmac_key`**: trocar invalida os códigos de recuperação existentes; peça aos usuários que os regenerem.
- **`api_session_secret`**: trocar encerra todas as sessões.
- **Senhas do banco**: crie os secrets novos, reimplante e rode o `migrate` (o `db_roles.sql` aplica `ALTER ROLE`);
  `postgres_password` só vale na criação do volume (para trocá-la use `ALTER ROLE` no banco e atualize o secret).

## 11. Parada de emergência e estados do robô

Painel: **Parada de emergência** (comando assinado). Sem painel:

```bash
docker exec $(docker ps -q -f label=com.docker.swarm.service.name=cryptoinvest_runner) \
  python -m engine.runner.paper emergency-stop --data-dir /var/lib/cryptoinvest/paper \
  --operator seu.nome --reason "motivo"
docker service scale cryptoinvest_runner=0      # derruba o processo (stops das posições permanecem no estado salvo)
```

`stop` encerra o processo com código 0 e o Swarm **não** o reinicia (`on-failure`); religar: `docker service scale
cryptoinvest_runner=1` (ou `docker service update --force`).

| Estado | O que fazer |
|---|---|
| `HALTED` | Trava de risco (perda diária/drawdown). Não retoma sozinho: leia o motivo no painel/`paper status`, aguarde a pausa mínima e use **Reativar** (exige reauth e confere o drawdown). `resume` nunca reativa HALTED. |
| `FAILED_PERSISTENCE` | O runner não consegue gravar o diário: pare de operar, **não** apague `paper-data`. Verifique disco (`df`), saúde do Postgres e o outbox (`outbox_pending` no status). Corrigida a causa, reinicie o runner: o outbox é reaplicado antes de recuperar o estado e o `start` recusa se o banco ainda estiver fora. |
| Stops cegos (`stops_blind`) | Há posição sem confirmação do stop acima de `--max-stop-blind-seconds`: o robô se pausa. Verifique conectividade com a Binance (rede `egress`, DNS, `PAPER_WS_URL`) e a reconciliação; se persistir, parada de emergência. |
| Runner `unhealthy` | `status.json` velho: veja `docker service logs cryptoinvest_runner`; o Swarm reinicia após falha. |

## 12. TLS, proxy e rate limit

- **Atrás de um balanceador/ACME do host** (Caddy, Traefik, cloud LB): use `stack.http.yml`; o balanceador termina HTTPS,
  encaminha para a porta 8080 do nó e **deve** enviar `X-Forwarded-For`. O proxy do stack enxerga o IP do balanceador,
  por isso `stack.http.yml` exige `TRUSTED_LB_CIDR` (IP/CIDR do balanceador; vários separados por vírgula): o
  entrypoint da imagem `web` gera `set_real_ip_from <CIDR>; real_ip_header X-Forwarded-For; real_ip_recursive on;`. Sem a
  variável (ou com valor inválido) o contêiner **falha ao subir** com mensagem clara. O `X-Forwarded-For` só é
  respeitado quando vem de um IP dentro de `TRUSTED_LB_CIDR`; de qualquer outro é ignorado (vale o IP da conexão).
  Confira com um login de teste (item do checklist).
- **HTTPS no próprio proxy**: `stack.tls.yml` + secrets `tls_cert` (cadeia completa PEM) e `tls_key`. Obtenha o certificado
  por ACME fora do stack (certbot DNS-01 ou `acme.sh`) e renove assim: crie o novo par como `..._tls_cert_v2`, ajuste
  `source` e reimplante. O nginx usa TLS 1.2/1.3 e HSTS. Aqui o cliente conecta direto ao proxy: não há `real_ip`
  (nem `TRUSTED_LB_CIDR`) e um `X-Forwarded-For` recebido nunca é acreditado.
- **Rede interna sem internet pública**: use CA própria/mkcert para o `tls.yml` e exponha o nó só na VLAN/VPN.
- O proxy só atende o `Host` igual a `PANEL_DOMAIN` (`server_name` fixo; qualquer outro recebe `return 444`, e o
  `/healthz` local segue respondendo); redirecionamento e CSP usam esse nome, nunca o `Host` recebido.
- O proxy sobrescreve `X-Forwarded-For` com o IP do cliente, repassa `Origin`/`Host`, limita o corpo a 64 KiB e aplica
  dois limites por IP no nível do server (não por `location`): `/api/auth` a 5 req/s (burst 20) e todo `/api/` a 30 req/s
  (burst 60), ambos além do throttle da própria API. As chaves usam o URI normalizado; formas não canônicas de
  `/api/auth` (`//`, `%2f`, `%61`, `./..`) são recusadas com 400 antes de chegar à API.
- `/assets/` só recebe `Cache-Control: immutable` em 200/304 (um 404 não fica em cache por um ano).
- `API_TRUST_FORWARDED_FOR=true` com `API_TRUSTED_PROXIES=["10.77.1.0/24"]` (só a rede `edge`, onde estão o proxy e a api).
  Em produção a API exige origens HTTPS, `cookie_secure` e HSTS (`API_PANEL_ORIGINS` vem de `PANEL_DOMAIN`).

## 13. Checklist de endurecimento

- [ ] Secrets criados com `deploy/secrets.sh`, cofre fora do servidor; nenhum `.env` ou segredo em disco no nó.
- [ ] Imagens com tag de versão (sem `latest`), construídas de commit limpo; varredura de CVEs (`trivy`/`docker scout`) feita **antes** de implantar.
- [ ] Nó atualizado, firewall só 443 (e 80 se `tls.yml`), SSH por chave; portas 2377/7946/4789 fechadas ao mundo (nó único: nem expostas).
- [ ] `docker stack config` validado e `docker stack ps` sem tarefas em loop de reinício.
- [ ] `migrate` concluído (`done`) e `check-grants` OK após **cada** atualização.
- [ ] Calendário de eventos com cobertura vigente (seção 1).
- [ ] Admin com 2FA ativo; `API_RECOVERY_ACCEPT_LEGACY_HASHES=false` (já no stack).
- [ ] Backup agendado, `restore.sh verify` executado e registrado; cópia externa.
- [ ] `trust_forwarded_for`/`real_ip` conferidos com um login de teste (auditoria mostra o IP do cliente, não o do proxy).
- [ ] Rotação de logs configurada; alertas externos ligados se desejado.
- [ ] Limites de recursos revisados (`deploy.resources`) para a máquina real.
- [ ] Confirmado: modo PAPER, `/live-gate` inelegível e `POST /live/activate` → 409.

## Limites conhecidos

- O Swarm não suporta `security_opt: no-new-privileges`; a mitigação é usuário não-root, `cap_drop: ALL` e raiz somente leitura
  em todos os serviços, inclusive banco (`70:70`, tmpfs em `/tmp` e `/var/run/postgresql`) e Redis (`999:1000`). O Swarm não
  aceita uid/gid em tmpfs: o tmpfs do socket nasce com modo 1777 e basta ao uid 70.
- **Dono do volume do banco**: o Timescale agora roda como `70:70` e o diretório de dados precisa pertencer a esse uid.
  Volume NOVO herda o dono da imagem (70:70) sem ajuste; volume EXISTENTE criado pelo contêiner anterior já é 70:70
  (o entrypoint rodava `chown` para o `postgres`; validado). Se o volume veio de restauração/cópia feita como root, ajuste
  uma vez, com o banco parado:
  `docker run --rm --network none --user 0 -v cryptoinvest_timescale-data:/d --entrypoint chown timescale/timescaledb:2.30.2-pg17 -R 70:70 /d`.
- Volumes nomeados são locais: com mais de um nó, mantenha o rótulo `cryptoinvest=primary` em um só.
- A ordem migrate → api/runner é por reinício, não por dependência.
