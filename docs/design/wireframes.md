# CryptoInvest - Wireframes e fluxos do painel (proposta para aprovação)

Status: PROPOSTA de design. Complementa `design-system.md`. Telas visualmente prototipadas (HTML + screenshots): Dashboard e Controle em `docs/design/mockup/`. As demais são wireframes em ASCII com descrição precisa.

Os dados exibidos foram escolhidos a partir do que o motor REALMENTE produz (leitura de `engine/runner/paper_robot.py`, `engine/application/risk/*`, `engine/runner/README.md`):

- **Estados do robô** (`BotState`): STARTING, RUNNING, PAUSED, FAILED_PERSISTENCE, HALTED, STOPPING, STOPPED.
- **Status/heartbeat** (`status.json` e evento `state`): `state`, `heartbeat_at`, `started_at`, `code_version`, `stops_blind`, `halted`/`halt_reason`, `paused`/`pause_reason`, **`pause_reasons[]`** (vários motivos simultâneos, com operador e hora), `equity` (marcado a mercado), `realized_equity`, `peak_equity`, `drawdown`, `day`, `day_pnl`, `daily_pause_until`, `target_reached_on`, `pair_pause_until{par}`, `open_positions`, `pending_entries`, `last_candle{par}`, `redis_healthy`, `allow_open_calendar`, `outbox_pending`, `persistence_error`.
- **Travas** (`RiskConfig`, com tetos rígidos): risco por operação padrão 0,5% (teto 1%); perda diária padrão 1% (teto 5%); drawdown máximo 5% (teto 5%, desliga o robô); meta diária 1% (pausa novas entradas ao atingir); posições abertas máx. 2; perdas seguidas por par 2 (pausa o par ≥ 24 h); exposição total padrão 50% (teto 80%); exposição por moeda 50%; fração máx. por posição 25%; retorno/risco mínimo 1:2; spread máx. 0,2%; volume 24 h mínimo 1 M; margem de ±30 min em eventos; idade máxima do sinal e da cotação; BNB bloqueada por padrão.
- **Decisões** (`Decision`): ação `enter/exit/hold/skip`, par, motivo (`código: mensagem`), origem (`risk`, estratégia), sinal associado. Códigos de rejeição reais (`RejectionCode`): `robot_halted, paused, daily_loss_pause, daily_target_reached, pair_paused, max_positions, position_already_open, adding_to_loser, coin_exposure, spread_too_wide, low_liquidity, event_window, history_gap, reward_risk, net_reward_risk, invalid_equity, quantity_below_min, notional_below_min, inconsistent_input, event_calendar_stale, entry_pending, total_exposure, quote_not_allowed, asset_not_allowed, signal_stale, quote_stale, market_incoherent, price_filter`, mais `stops_blind`.
- **Controle do operador**: pausar, retomar (operador + motivo obrigatórios), parar (ordenado), parada de emergência (com liquidação opcional; operador + motivo obrigatórios), reativar após HALT (operador + motivo). Tudo vai para `control_events`/`audit_log`.
- **Trava do Live** (`evaluate_live_gate`): critérios por métrica (dias, drawdown, profit factor, trades, perdas, perda diária) e o critério `eligible_for_live`, reprovado quando o run usou `--allow-open-calendar` ("INELEGÍVEL: calendário aberto", para sempre).
- Eventos em tempo real: canais `decisions, orders, trades, state, equity, heartbeat, alerts, risk`. O painel assina via WebSocket e usa TanStack Query para o que é histórico.
- Autenticação/2FA e papéis virão da API da Fase 4 (o canal de comandos atual não autentica ninguém, conforme o README).

---

## 0. Estrutura geral (todas as telas)

### 0.1 Barra superior fixa (a resposta dos 3 segundos)

```
DESKTOP (altura 72, sempre visível, ocupa a largura do conteúdo; faixa de 4 px no topo da janela: AZUL no Paper, VERMELHA 6 px no Live)
+--------------------------------------------------------------------------------------------------------------+
| (●) Operando  aguardando sinal   [◌ PAPER simulado]        Saldo (simulado)   Resultado do dia    [■ Parar tudo]|
|                                                             10.184,32 USDT    ▼ −30,56 (−0,30%)               |
+--------------------------------------------------------------------------------------------------------------+
```

Leitura em 3 segundos, da esquerda para a direita: (1) **estado** em palavra + cor + ponto pulsante + motivo curto; (2) **modo** como selo (PAPER tracejado azul / LIVE cheio vermelho / BACKTEST pontilhado roxo); (3) **saldo** (com "(simulado)" no Paper); (4) **resultado do dia** com ▲/▼; (5) **Parar tudo**, sempre no canto direito, nunca desloca de lugar.

Regras:
- O estado usa as 5 faces visuais (ver mockup `estados.html`): verde pulsante "Operando"; âmbar pulsante "Pausado: <motivo>" ou "Stops cegos"; vermelho fixo "DESLIGADO por drawdown" ou "Falha ao gravar"; cinza "Parado"; STARTING/STOPPING em cinza com "Iniciando…/Encerrando…".
- Sem conexão com a API/WebSocket: o estado vira cinza tracejado "SEM CONEXÃO COM O PAINEL - dados de há 14 s" e o saldo e resultado ficam esmaecidos com o horário do último dado. Nunca mostrar o último estado como se fosse atual. Heartbeat velho (> 90 s sem `heartbeat_at`) vira âmbar "Robô sem sinal de vida".
- Todo valor monetário do Paper leva "(simulado)" no rótulo; no Live o rótulo é "Saldo (real)". Em LIVE o saldo e o P&L ganham o ícone de escudo e a janela tem moldura vermelha.
- Parar tudo permanece visível e acionável em QUALQUER estado, inclusive HALTED, FAILED_PERSISTENCE e sem conexão (neste último, o botão mostra "Parar tudo (sem conexão: usa o canal local)" ou fica desabilitado com explicação, a decidir com a engenharia; ver decisão D6).

```
TABLET (820): 2 linhas
+--------------------------------------------------------------+
| (●) Operando aguardando sinal   [◌ PAPER simulado]           |
| Saldo (simulado) 10.184,32   Resultado do dia ▼ −30,56 (−0,30%)   [■ Parar tudo] |
+--------------------------------------------------------------+
CELULAR (390): 2 linhas
+--------------------------------------+
| (●) Operando [◌ PAPER]   [■ Parar tudo]|
| Saldo (simulado)      Resultado do dia |
| 10.184,32 USDT        ▼ −30,56 (−0,30%)|
+--------------------------------------+
```

### 0.2 Menu lateral recolhível

```
Expandido (248)            Recolhido (72, ícones + tooltip)      Celular (barra inferior)
[C] CryptoInvest           [C]                                    [Início][Controle][Operações][Desempenho][Mais]
 ▣ Dashboard                ▣                                      "Mais" abre Sheet: Mercado, Decisões ao vivo,
 ⇅ Controle  (1)            ⇅ •                                     Backtest, Configurações, Tema, Sair
 ▮ Mercado                  ▮
 ≡ Decisões ao vivo         ≡
 ⇄ Operações                ⇄
 ⌁ Desempenho               ⌁
 ◷ Backtest                 ◷
 ⚙ Configurações            ⚙
 ⊟ Recolher menu            ⊟
```

- Selo numérico âmbar no item (ex.: Controle (1)) quando há alerta/pausa ativa; vem com texto para leitor de tela ("1 alerta").
- Item ativo: fundo azul-claro, barra lateral de 3 px e `aria-current="page"`. Estado recolhido persiste por usuário. Abaixo de 1100 px recolhe sozinho. Tema claro/escuro em um clique: botão na página (protótipo) e item "Tema" no rodapé do menu/"Mais".
- Ordem do menu por frequência de uso: o que o operador olha mais (Dashboard, Controle) em cima; Configurações por último.

### 0.3 Estados globais (valem para toda tela)

| Estado | Como aparece | Onde |
|---|---|---|
| Carregando | Skeleton do tamanho do conteúdo final; barra superior mostra "Conectando…" cinza | Cards, tabelas, gráficos |
| Vazio | Ícone + frase + ação sugerida; nunca durante o carregamento | Listas, tabelas |
| Erro de dados | Faixa âmbar/vermelha dentro do card, com "Tentar de novo" e hora da última atualização; o restante da tela segue vivo | Por card, não por página |
| Desconectado (painel ↔ API) | Faixa fixa abaixo da barra superior: "Sem conexão. Os números abaixo são de 14:02:11 e podem estar desatualizados. Tentando reconectar…" e todos os números ficam esmaecidos com selo "desatualizado" | Todas |
| Robô pausado (PAUSED) | Barra superior âmbar com motivo; faixa na tela com `pause_reasons` (cada motivo, operador, hora) e botão "Retomar" (pede nome e motivo) | Dashboard, Controle |
| HALTED | Barra superior vermelha "DESLIGADO por drawdown"; Dashboard mostra hero vermelho "O robô desligou porque a queda chegou a 5%. Ele não volta sozinho." + "Reativar" (nome e motivo) | Dashboard, Controle |
| FAILED_PERSISTENCE | Barra vermelha "Falha ao gravar - entradas bloqueadas; stops ativos"; hero vermelho explica "Nada foi perdido: N eventos aguardam na fila (`outbox_pending`)". Volta sozinho; mostra tentativa e último erro em linguagem simples | Dashboard, Controle |
| Stops cegos (`stops_blind`) | Barra âmbar "Stops cegos - sem dados de mercado"; hero âmbar: "Sem preços há X min. Novas entradas pausadas; seus stops continuam ativos. Se passar de 5 min com posição aberta, você será avisado." (corresponde a `STOPS_BLIND_PROLONGED`) | Dashboard, Controle, Mercado |
| Run inelegível | Selo âmbar "Run inelegível para o Live (calendário aberto)" no Dashboard, Controle e Desempenho (ver 0.4) | Várias |

### 0.4 Mensagens transversais pedidas

**"Ficar parado em USDT é uma decisão válida."** Aparece (a) no hero do Dashboard quando não há posição ("Nenhuma posição aberta. 100% em USDT. Ficar parado é uma decisão válida: o robô só entra quando passa nos filtros de risco."), (b) no estado vazio de Operações, (c) como linha de resumo em Decisões ao vivo ("Hoje: 8 sinais, 1 entrada, 7 esperas - isso é normal"), (d) nas métricas de Desempenho como "Tempo parado: 76% (não é falha)", e (e) ao primeiro acesso. Tom neutro-verde, ícone ✓; NUNCA vermelho, NUNCA "inativo/ocioso".

**Status das travas de risco**, sempre com a frase "valor atual de limite" e uma palavra de estado: "Perda diária: 0,3% de 1% - Folgado"; "Drawdown: 1,8% de 5% - Folgado"; "Perdas seguidas (ETH/USDT): 1 de 2 - Atenção"; "Posições abertas: 1 de 2"; "Exposição total: 24% de 50%"; "Meta do dia: 0,0% de 1% (ao atingir, o robô para de abrir operações)". Trava ativada vira vermelho com "ATIVA desde 14:02 até 00:00 UTC".

**Selo "Run inelegível para o Live (calendário aberto)"**: caixa âmbar com ícone ⚠, título e uma frase de causa e consequência ("Este teste rodou sem o calendário de eventos. Ele nunca vai poder liberar dinheiro real; é preciso começar um novo teste com o calendário."). Persistente, não fechável, visível no Dashboard (card "Caminho para o Live"), Controle (Trava do Live), Desempenho (cabeçalho) e como linha na lista de runs. Também aparece como chip âmbar "Calendário aberto" na Saúde da conexão (`allow_open_calendar`).

**Simulado (paper) vs. real (live)** - regra de marcação: (1) selo de modo na barra superior; (2) faixa superior da janela azul (4 px) ou vermelha (6 px) + moldura vermelha em torno da janela no Live; (3) "(simulado)"/"SIM" tracejado azul em saldos, KPIs, tabelas e gráficos do Paper; no Live, "(real)" com escudo; (4) cards, linhas de tabela e itens de histórico carregam o selo de modo do run a que pertencem (histórico mistura runs: cada linha mostra PAPER/LIVE/BACKTEST); (5) títulos de diálogos de ação dizem o modo em destaque; (6) o PDF/CSV exportado leva o modo no nome do arquivo e no cabeçalho; (7) alternar para Live nunca é por toggle (ver 0.5).

### 0.5 Trava do Live (critérios e frase digitada)

```
+-- Trava do Live (dinheiro real) ----------------------------------------------------[🔒 Bloqueado]--+
| ⚠ Run inelegível para o Live (calendário aberto)  - causa e consequência em 1 frase                  |
|                                                                                                      |
| ◷ Pelo menos 60 dias de teste no Paper ........................................ 23 de 60            |
| ✓ Drawdown máximo abaixo de 5% ................................................ 1,8%                |
| ✕ Profit factor acima de 1,5 .................................................. 1,34               |
| ◷ Pelo menos 30 operações ..................................................... 17 de 30            |
| ✓ Pelo menos 5 perdas na amostra (sem perdas não dá para medir o risco) ....... 6 de 5              |
| ✕ Run elegível (calendário de eventos ligado o tempo todo) .................... Inelegível          |
|   (também: perda diária nunca acima do limite)                                                       |
|                                                                                                      |
| Para liberar, digite exatamente: OPERAR COM DINHEIRO REAL                                            |
| [ desabilitado até todos os critérios ✓ ]       Código do app autenticador (2FA): [ ______ ]        |
| [🔒 Liberar Live]                                                                                    |
+------------------------------------------------------------------------------------------------------+
```

- Critérios: 60 dias, drawdown < 5%, profit factor > 1,5, 30 trades, 5 perdas (e `eligible_for_live`). Cada critério mostra valor atual e meta, com ícone: ✓ cumprido, ✕ reprovado (medido e ruim), ◷ ainda em andamento (sem culpa). "Faltam 37 dias" em vez de só "23 de 60".
- Só quando TODOS ✓: o campo da frase habilita. A frase `OPERAR COM DINHEIRO REAL` precisa ser digitada, sem copiar/colar (colar desabilitado) e com correspondência exata; mostra ✓ ao acertar. Depois, pede o código 2FA e então abre um diálogo final com resumo (capital que será exposto, limites de risco vigentes, chaves da Binance conectadas, "nada pode ser desfeito sozinho; Parar tudo continua disponível") e botão "Ligar Live (dinheiro real)". Ao confirmar, a janela inteira muda para a moldura vermelha e o selo LIVE; o primeiro dia no Live inicia com risco reduzido (decisão D4 do cliente).
- Reverter para o Paper e Parar tudo NÃO exigem frase nem 2FA (reduzem risco).
- Quem ver: somente o papel "administrador" (decisão D5).

---

## 1. Login com 2FA (TOTP)

**Objetivo:** entrar com segurança sem fricção desnecessária. **Hierarquia:** logotipo, formulário único e centrado, ajuda. **Dados:** e-mail, senha, código de 6 dígitos.

```
+----------------------------------------+
|              [C] CryptoInvest          |
|         Entre para ver o seu robô      |
|                                        |
|  E-mail                                |
|  [______________________________]     |
|  Senha                                 |
|  [____________________] [👁 Mostrar]  |
|  [ Continuar ]                         |
|                                        |
|  --- passo 2 (mesma tela, troca o card) ---
|  Código do aplicativo autenticador     |
|  Digite os 6 números do app            |
|  [ _ ][ _ ][ _ ][ _ ][ _ ][ _ ]        |
|  [ Entrar ]   Usar código de recuperação|
|  Não encontra sua conta? Fale com o admin.  |
+----------------------------------------+
```

- **Passos:** 1) e-mail e senha; 2) código TOTP. Foco inicial no e-mail; no passo 2, no primeiro dígito; os 6 campos aceitam colar o código inteiro; `autocomplete="one-time-code"` e `inputmode="numeric"`; envio automático ao completar 6 dígitos; Enter envia.
- **Estados:** carregando (botão desabilitado com indicador "Entrando…"); erro genérico sem revelar qual campo falhou ("E-mail, senha ou código não conferem. Confira e tente de novo."); código expirado ("Esse código já venceu. Use o próximo que aparecer no app."); bloqueio temporário após N tentativas ("Muitas tentativas. Tente de novo em 5 min.", com contagem); sem conexão ("Não conseguimos falar com o servidor. Verifique a internet."). Sessão expirada leva ao login com "Sua sessão terminou por segurança" e retorna à tela anterior.
- **Segurança em UI:** mostrar/ocultar senha, sem dica de qual conta existe, mensagem de código de recuperação (10 códigos mostrados uma vez, no cadastro do 2FA).
- **Desktop/tablet/celular:** card de 400 px centrado; no celular ocupa a largura com margem de 16 px; botão 44+ px.
- **Modo:** a tela de login não mostra saldo nem estado do robô (sem dados antes da autenticação).

## 2. Primeiro acesso (onboarding): conectar Binance -> configurar risco -> iniciar no Paper

**Objetivo:** levar uma pessoa leiga a um robô rodando em PAPER em ≤ 5 min, sem possibilidade de ir direto ao Live. Assistente de 3 passos (depois do 2FA obrigatório) com progresso fixo no topo ("Passo 2 de 3").

```
Passo 0  Boas-vindas / 2FA (obrigatório antes de tudo)
Passo 1  Conectar a Binance         (opcional para Paper; obrigatório só para o Live)
Passo 2  Configurar o risco         (3 perguntas simples + resumo)
Passo 3  Iniciar no Paper           (revisão e botão)

+------------------------------------------------------------------+
| Passo 1 de 3  ●────○────○     Você começa em PAPER (dinheiro de mentira) |
| Conectar a Binance                                                |
| Para testar não precisa de chaves. Você pode pular e conectar depois. |
| (Quando conectar, use chaves SÓ de leitura + trade spot, SEM saque.)  |
| API Key  [__________________]                                     |
| Secret   [__________________] [👁]                                |
| ✓ Só leitura e spot   ✕ Saque desligado   (checagem automática)   |
| [Testar conexão]        [Pular por agora]            [Continuar]  |
+------------------------------------------------------------------+
| Passo 2 de 3  Configurar o risco                                  |
| Quanto você aceita perder em UMA operação?   (•) 0,5% (recomendado) ( ) 0,25% ( ) 1% |
|   "Com 10.000 USDT, cada perda custa no máximo 50 USDT."          |
| Quanto você aceita perder em UM DIA?         [ 1% ]   (máx 5%)    |
| Em que queda o robô se desliga sozinho?      [ 5% ] fixo          |
| Resumo: "O robô para o dia se perder 1%, desliga se cair 5% desde o pico." |
| [Voltar]                                          [Continuar]     |
+------------------------------------------------------------------+
| Passo 3 de 3  Iniciar no Paper                                     |
| [◌ PAPER simulado] Pares: BTC/USDT, ETH/USDT · 1 hora · capital simulado 10.000 USDT |
| ⚠ Calendário de eventos: [ligado ✓] (se desligado: o teste fica inelegível para o Live, "para sempre") |
| "Ficar parado em USDT é normal. O robô só entra quando o risco compensa." |
| [Voltar]                                  [Iniciar no Paper ▶]    |
+------------------------------------------------------------------+
```

- Chaves: campos nunca ecoam o segredo depois de salvo; só mostra "Chave …a91f conectada". Validação em tempo real (formato, permissões), com recusa clara se a chave permitir saque ("Esta chave pode sacar dinheiro. Crie outra sem a permissão de saque."). Segredos ficam no servidor, nunca em `localStorage`.
- Valores aceitos respeitam tetos do motor (risco ≤ 1%, perda diária ≤ 5%); campo acima do teto mostra erro com a regra.
- Estados: carregando (testando conexão), erro (chave inválida, sem rede, relógio fora de sincronia), sucesso (✓ com saldo da conta de leitura), saída segura ("Salvar e continuar depois").
- Desktop: card central 640 px com resumo lateral; tablet: igual; celular: um passo por tela, botões fixos no rodapé.
- Ao terminar, cai no Dashboard com um aviso "Seu robô está rodando em PAPER" e um pequeno tour opcional (3 dicas: estado, travas, Parar tudo).

## 3. Dashboard (prototipado)

**Objetivo:** responder "está tudo bem?" e "como estou?" em 3 s, e mostrar o que o robô está fazendo e por que não faz.
**Protótipo:** `mockup/dashboard.html` e `mockup/screenshots/dashboard-*.png`.

```
DESKTOP 1440
[ Barra superior fixa ......................................................................... ]
Dashboard                                                                   [Tema escuro/claro]
[ EXEMPLO - dados fictícios ]
+-- HERO de estado (verde/âmbar/vermelho conforme estado) ---------------------------------------+
| ✓ Tudo certo: o robô está ligado e esperando uma boa oportunidade.                              |
|   Hoje analisou 8 sinais e entrou em 1. Ficar parado em USDT é uma decisão válida - 76% em USDT |
+-------------------------------------------------------------------------------------------------+
[KPI Patrimônio (simulado)] [KPI Resultado do dia] [KPI Drawdown atual] [KPI Operações abertas 1 de 2]
+-- Travas de risco -----------------------------+ +-- Caminho para o Live  [PAPER] ---------------+
| Perda diária        0,3% de 1%   ▓░░░ Folgado  | | ⚠ Run inelegível para o Live (calendário…)   |
| Drawdown            1,8% de 5%   ▓░░░ Folgado  | | ◷ Dias 23/60  ✓ DD 1,8%  ✕ PF 1,34  ◷ 17/30   |
| Perdas seguidas ETH 1 de 2       ▓▓░░ Atenção  | | ✓ 6 de 5 perdas          [Ver no Controle]    |
| Posições 1 de 2 · Exposição 24% de 50%         | +-----------------------------------------------+
+------------------------------------------------+
+-- Curva de capital [SIMULADO] (Recharts) ------+ +-- Últimas decisões ---------------------------+
| linha 30 dias + capital inicial tracejado       | | 14:00 BTC/USDT ↑ Comprou  barra elefante...    |
+-------------------------------------------------+ | 14:00 ETH/USDT ‖ Não entrou event_window      |
+-- Posição aberta [SIMULADA] ----------------------------------------------------------------------+
| Par | Entrada | Stop | Alvo | Tamanho | Resultado (+0,6 R) | Proteção: ✓ Stop ativo                  |
```

- **Hierarquia:** 1) estado (barra + hero); 2) saldo e resultado do dia; 3) travas (o quão perto do limite); 4) o que o robô está fazendo (posições, decisões); 5) caminho para o Live.
- **Dados (reais no motor):** `state`, `pause_reasons`, `equity`, `peak_equity`, `drawdown`, `day_pnl`, `open_positions`, travas de `RiskConfig`/`RiskState`, `loss_streaks`, `pair_pause_until`, decisões recentes, posição aberta (entrada/stop/alvo/tamanho), critérios do `evaluate_live_gate`.
- **Hero por estado:** RUNNING sem posição ("Ficar parado em USDT é uma decisão válida"); RUNNING com posição ("1 operação aberta, protegida por stop"); PAUSED (âmbar, com motivos); HALTED (vermelho, "Reativar"); FAILED_PERSISTENCE (vermelho, fila de eventos); stops cegos (âmbar); STOPPED (cinza, "Iniciar"); SEM CONEXÃO (cinza).
- **Estados:** carregando (skeleton nos KPIs, barras e gráfico; hero mostra "Conectando ao robô…"); vazio (primeiro dia: "Ainda sem operações. Normal." no card de posição); erro (card isolado com "Tentar de novo"); desconectado (faixa + esmaecido); pausado/HALTED/FAILED_PERSISTENCE/stops cegos (hero e barra, ver 0.3).
- **Tablet (820):** menu em trilho; KPIs 2×2; Travas e Live empilham; gráfico e decisões empilham; tabela rola na horizontal.
- **Celular (390):** barra superior em 2 linhas; hero compacto; KPIs 2 colunas; ordem: hero, KPIs, travas, caminho para o Live, gráfico, decisões, posição; barra inferior de navegação.
- **Verificado visualmente:** ver screenshots `dashboard-{escuro,claro}-{1440,820,390}.png`.

## 4. Controle (prototipado)

**Objetivo:** operar e entender o robô: ligar/pausar/parar, ver saúde e travas, e manter a trava do Live. **Protótipo:** `mockup/controle.html` e screenshots `controle-*`.

```
Hero de estado (igual ao Dashboard, com "ligado desde", run id, última conferência)
+-- Liga / desliga [PAPER] ------------------+ +-- Saúde da conexão ----------------------------+
| [●━ ] Robô ligado (explicação 1 frase)      | | Dados de mercado ✓  Stops cegos ✓ Não          |
| [‖ Pausar novas entradas] [Retomar]         | | Gravação no banco ✓  Batimento ✓  Calendário ⚠  |
| Pausas ativas: (vazio: ✓ Nenhuma pausa)     | | Conferência de posições ✓                      |
+---------------------------------------------+ +------------------------------------------------+
+-- Travas de risco ---------------------------+ +-- Parar tudo (emergência) ---------------------+
| (as mesmas barras do Dashboard)              | | Diálogo de confirmação (ver abaixo)            |
+---------------------------------------------+ +------------------------------------------------+
+-- Trava do Live (dinheiro real) --- (seção 0.5) -------------------------------------------------+
+-- Estados do robô em palavras simples (tabela) --+ +-- Histórico de controle (timeline) --------+
```

- **Parar tudo - diálogo:** modo em destaque ("PAPER simulado"/"LIVE dinheiro real"); duas opções excludentes: "Só bloquear novas operações" (padrão, recomendada: mantém posições com stop) ou "Bloquear e vender tudo a mercado" (mostra o que será vendido, ex. "BTC/USDT 0,0231, perda possível por preço pior"); nome e motivo; "Confirmar: parar tudo" (vermelho) e "Voltar (Esc)". No Live, texto extra "Isto vende em dinheiro real." e confirmação com clique extra na opção de venda. Depois: toast + estado vira Parado/Pausado, com timeline registrada.
- **Pausas ativas** (`pause_reasons`): lista com motivo, quem, quando e botão "Retomar" por motivo quando permitido (a pausa por perda diária só termina à meia-noite UTC: botão desabilitado com "libera às 00:00 UTC"; a pausa por divergência exige conferência e nome/motivo).
- **Reativar após HALTED:** único caminho para religar após drawdown 5%; botão "Reativar" abre diálogo "Você revisou o que aconteceu?" com nome e motivo obrigatórios e link ao histórico do run; registra em `audit_log`.
- **Dados:** `state`, `started_at`, `heartbeat_at`, `stops_blind`, `outbox_pending`, `persistence_error` (traduzido), `redis_healthy`, `allow_open_calendar`, `pair_pause_until`, `daily_pause_until`, `target_reached_on`, auditoria (`halted`, `reactivated`, `paused`, `resumed`, `daily_loss_pause`, `daily_target_reached`, `pair_paused`).
- **Estados:** carregando (skeleton); vazio (pausas ativas: "Nenhuma pausa ativa"); erro (comando falhou: toast vermelho com "Não confirmado — confira o Histórico de controle" (5xx/timeout) ou "Nada mudou" (4xx claro) e botão tentar de novo; comandos expiram em 10 min e o painel avisa); desconectado (botões de ação desabilitados com explicação, exceto Parar tudo conforme D6); pausado/HALTED/FAILED_PERSISTENCE/stops cegos (hero + saúde destacando o item causador).
- **Tablet/celular:** cards em 1 coluna; no celular, a tabela de estados vira lista de cartões e o diálogo é uma Sheet de baixo para cima.

## 5. Mercado

**Objetivo:** ver o que o robô vê: candles dos pares, indicadores e a vela em que o sinal ocorreu. **Hierarquia:** seletor de par/tempo, gráfico, painel lateral do que o robô pensa.

```
[BTC/USDT ▾] [1h ▾]  Último 67.485,20 ▲ +0,4%   dados há 3 s ✓      [Mostrar: ☑ EMAs ☑ Barras elefante ☑ Stops/alvos]
+-- Candles (Lightweight Charts) --------------------------------------+ +-- O que o robô vê (BTC/USDT) ----+
| velas + volume; marcadores: ▲ entrada, ▼ saída, ■ stop, ◆ alvo;       | | Tendência: lateral               |
| barra elefante destacada com contorno; linha do stop/alvo da posição  | | Sinal: nenhum agora              |
| aberta; faixa cinza nas janelas de evento (calendário)                | | Última decisão: "esperou"        |
+-----------------------------------------------------------------------+ | Spread 0,01% ✓ Volume 24h ✓      |
                                                                           | Posição: ver Operações           |
```

- **Dados:** velas fechadas (o robô só decide em vela fechada), volume, cotação, spread, volume 24 h, idade da cotação (`quote_stale`), `last_candle{par}`, `stops_blind` por par, janelas de evento.
- **Estados:** carregando (skeleton do gráfico com eixos); vazio (par sem histórico: "Ainda sem velas para este par. Carregando histórico…" + ação); erro (feed caiu: faixa âmbar "Sem preços há 2 min" com o mesmo aviso de stops cegos); desconectado; stops cegos = linha vermelha tracejada "sem dados" sobre o gráfico a partir da última vela.
- **Desktop:** gráfico 2/3 + painel 1/3. **Tablet:** painel abaixo. **Celular:** gráfico em largura total, 280 px de altura, painel em abas ("Gráfico | O que o robô vê"); zoom por pinça; rótulos de eixo reduzidos.
- **Acessibilidade:** gráfico com resumo (`aria-label`) e tabela alternativa das últimas 20 velas.
- Marcar tudo do Paper como simulado (marcadores com tracejado azul) e do Live como real.

## 6. Decisões ao vivo

**Objetivo:** transparência: por que o robô entrou, esperou ou recusou. É a tela que gera confiança em "ficar parado".

```
Hoje: 8 sinais · 1 entrada · 7 recusas/esperas - isso é normal.    [Todos ▾] [Par ▾] [Só recusas ☐]  ● ao vivo
+-----------------------------------------------------------------------------------------------+
| 14:00  BTC/USDT  ↑ Comprou   Barra elefante + rompimento · risco 0,5% · alvo 2 R   [▸ detalhes]|
| 14:00  ETH/USDT  ‖ Não entrou Há evento de mercado nos próximos 30 min             event_window|
| 13:00  BTC/USDT  ◌ Esperou    Sem sinal: mercado lateral                                       |
| 12:00  ETH/USDT  ‖ Não entrou Retorno/risco 1:1,4; mínimo é 1:2                    reward_risk |
| ▾ detalhes: sinal (entrada, stop, alvo, confiança, RSI), gate de risco (cada regra ✓/✕),       |
|   tamanho calculado, o que teria acontecido se entrasse ("operação evitada": +0,8 R / −1 R)    |
+-----------------------------------------------------------------------------------------------+
```

- **Dados:** `Decision` (ação enter/exit/hold/skip, par, motivo, origem, sinal), código de rejeição e mensagem, resultado hipotético quando existir ("operações evitadas" do backtest), hora da vela.
- **Tradução dos códigos** (tabela no app, tooltip no código): `event_window` "Perto de notícia importante"; `reward_risk` "Ganho possível pequeno demais para o risco (mínimo 1:2)"; `daily_loss_pause` "Limite de perda do dia atingido"; `daily_target_reached` "Meta do dia atingida; parou para proteger o ganho"; `max_positions` "Já há 2 operações abertas"; `position_already_open` "Já está comprado neste par"; `pair_paused` "Par em pausa de 24 h após perdas seguidas"; `spread_too_wide` "Diferença compra/venda grande demais"; `low_liquidity` "Pouco volume negociado"; `total_exposure`/`coin_exposure` "Já está muito exposto"; `stale/quote_stale` "Preço ou sinal velho demais"; `event_calendar_stale` "Sem calendário de eventos válido"; `robot_halted`/`paused` "Robô desligado/pausado"; `stops_blind` "Sem dados de mercado".
- **Cores e ícones:** Comprou/Vendeu (verde ↑/saída ↓ conforme resultado), Não entrou (âmbar ‖), Esperou (cinza ◌). Nunca vermelho para "não entrou" (não é erro).
- **Estados:** carregando (5 linhas skeleton); vazio ("Ainda nenhuma decisão. O robô decide a cada vela fechada (a cada 1 hora)."); erro; desconectado (lista congelada com selo "pausado: sem conexão" e botão "Retomar ao vivo"); novas decisões entram no topo com realce de 600 ms; se o usuário rolou, aparece botão "2 novas decisões".
- **Desktop:** lista + filtros fixos; **tablet:** igual; **celular:** cartões com detalhes em Sheet.

## 7. Operações

**Objetivo:** posições abertas, ordens pendentes e histórico de operações encerradas, com resultado em R e em USDT.

```
[Abertas (1)] [Ordens pendentes (0)] [Encerradas (17)]            [Período ▾] [Par ▾] [Modo ▾] [Exportar CSV]
Abertas:  Par | Modo | Entrada | Stop | Alvo | Tamanho | Resultado (R / USDT) | Tempo aberta | Proteção (OCO ✓)
Encerradas: Data | Par | Modo | Estratégia | Entrada→Saída | Motivo da saída | Resultado R | USDT | Taxas | ▸ linha do tempo
▾ Linha do tempo (expansível): sinal → ordem limite → preenchimento → parcial 50% → stop movido p/ empate → saída
```

- **Dados:** `Order`, `Fill`, `Position`, `Trade`; motivos de saída (stop, alvo, trailing_stop, breakeven_stop, limite de tempo, emergência); parciais e OCO.
- **Estados:** vazio ("Nenhuma operação aberta. Ficar parado em USDT é uma decisão válida." + [Ver decisões ao vivo]); carregando (skeleton de 6 linhas); erro; sem conexão; caso crítico: posição sem stop ativo (`reconcile` divergente) = linha âmbar com ⚠ "Sem stop ativo - robô pausado" e destaque na barra superior.
- **Modo:** coluna e selo por linha. Filtro "Modo" padrão = o modo atual; misturar Paper e Live em totais é proibido (totais separados).
- **Tablet:** colunas menos importantes ocultas (taxas, estratégia), expansíveis na linha. **Celular:** cartões por operação com resultado grande, expandir para a timeline.
- Valores negativos com ▼ e "−"; positivos com ▲; resultados em R e em USDT lado a lado.

## 8. Desempenho

**Objetivo:** responder "está funcionando de verdade?" com prioridade a drawdown e consistência, depois lucro (a prioridade do próprio motor).

```
[Modo: PAPER ▾] [Período: 30 dias ▾]     ⚠ Run inelegível para o Live (calendário aberto)
KPIs: Retorno | Drawdown máx. | Profit factor | Expectativa (R) | Taxa de acerto | Payoff | Sharpe | Tempo parado
+-- Curva de capital + drawdown (Recharts: área) -----------------------+ +-- Resultado por mês ---------+
+-- Distribuição de resultados em R (histograma) -----------------------+ +-- Por estratégia / par -------+
+-- Paper x Backtest no mesmo período (comparação) ---+ +-- Aviso de amostra: "17 operações: poucas para concluir" --+
```

- **Dados** (`compute_paper_metrics`/relatórios): retorno, drawdown máximo, meses positivos, taxa de acerto, profit factor, payoff, expectativa em R, Sharpe (retornos diários UTC, anualizado), tempo parado, resultado por mês e por estratégia, comparação paper x backtest nos mesmos candles, notas de honestidade.
- **Honestidade na UI:** toda métrica tem tooltip (glossário); aviso de amostra pequena (< 30 trades: "inconclusivo") em âmbar; "Tempo parado: 76% (não é falha)"; Backtest sempre com selo BACKTEST roxo e premissas (taxa, deslizamento, latência) visíveis.
- **Estados:** vazio ("Ainda sem dados suficientes. Os gráficos aparecem após a primeira operação encerrada."), carregando (skeleton por gráfico), erro por card, desconectado (dados históricos continuam; só o "ao vivo" some).
- **Desktop:** 4 KPIs por linha, 2 colunas de gráficos. **Tablet:** KPIs 2 por linha. **Celular:** KPIs rolam em carrossel de 2; gráficos com altura fixa de 200 px e tabela de dados alternativa.
- Exportar relatório (PDF/CSV) com modo no cabeçalho.

## 9. Backtest (assistente em etapas)

**Objetivo:** testar uma ideia no passado, sem risco, e ler o resultado com honestidade. O selo BACKTEST (roxo, pontilhado) aparece em todas as etapas, e o resultado nunca usa o verde de "ganho real".

```
Etapas:  1 Escolher  ●──  2 Premissas  ○──  3 Revisar  ○──  4 Rodar e ver resultado
1 Escolher: Pares [BTC/USDT ☑ ETH/USDT ☑]  Tempo [1h ▾]  Período [últimos 2 anos ▾]  Estratégias [☑ Barra elefante ☑ Cruzamento de EMAs]
            Teste em janelas (walk-forward) [☑]  "Recomendado: confere o resultado fora da amostra"
2 Premissas: Capital [10.000] Taxa [0,10%] Deslizamento [5 pontos-base] Latência [1 vela]  (valores padrão do motor, com explicação)
            Parâmetros: confiança mín., RSI mín., retorno/risco  [grade pequena] ⚠ "Quanto mais combinações, maior o risco de enganar-se (sobreajuste)"
3 Revisar:  resumo em frases + estimativa de duração + [◷ BACKTEST histórico]  [Rodar backtest ▶]
4 Resultado: progresso (barra) → relatório: retorno, DD, meses positivos, PF, payoff, R, Sharpe, tempo parado,
             operações evitadas (perda evitada x ganho perdido), veredito do walk-forward em 4 categorias:
             inconclusivo (amostra pequena) / sem edge detectável fora da amostra / indícios de sobreajuste / consistente
             [Baixar relatório] [Refazer com outras premissas]
```

- **Dados:** parâmetros da CLI (`--symbol`, `--timeframe`, `--years`, `--strategies`, `--params`, `--capital`, `--taker-fee`, `--maker-fee`, `--slippage-bps`, `--latency`, `--walk-forward`, `--train-days`, `--test-days`, `--grid`), métricas, `trades`, `roundtrips`, curva, `decisions`, veredito.
- **Estados:** carregando (progresso por etapa: baixando velas, simulando, calculando; botão "Cancelar"); vazio (sem execuções: "Rode seu primeiro backtest em 4 passos."); erro (sem dados suficientes: "Há um buraco nos dados em 12/03; usamos o maior trecho contínuo."; período menor que treino + teste: aviso do próprio motor); sem conexão (execução continua no servidor; painel reconecta e retoma a barra).
- **Validação em tempo real:** período mínimo para walk-forward (treino + teste); par não suportado; premissas fora da faixa.
- **Desktop:** etapas à esquerda (trilho vertical) + conteúdo; **tablet:** trilho horizontal; **celular:** uma etapa por tela e resultado em abas (Resumo, Gráficos, Operações).
- **Aviso fixo no resultado:** "Resultado de simulação no passado. Não garante o futuro." sem alarmismo; o veredito "inconclusivo" é mostrado em cinza/âmbar, não em vermelho.

## 10. Configurações (abas: Pares, Estratégias, Risco, IA, Conta/Chaves)

Layout: abas no topo (desktop/tablet) e lista de seções no celular; botão "Salvar alterações" fixo no rodapé do card, desabilitado sem mudanças ("Nada para salvar"); aviso "Alterações valem a partir da próxima vela; posições abertas mantêm o stop atual" (e o motor nunca reduz stop).

| Aba | Conteúdo (dados reais) | Pontos de UX |
|---|---|---|
| **Pares** | Lista de pares permitidos (ativar/pausar por par), ativos permitidos/bloqueados (BNB bloqueada por padrão), moeda de cotação (USDT), pausa do par (`pair_pause_until`), filtros da corretora (tick, step, min notional) | Switch por par com explicação; pausar par com posição aberta avisa "a posição segue protegida"; ao incluir par novo, "carregando histórico (≥ 20 velas)" |
| **Estratégias** | Barra elefante e cruzamento de EMAs: ligar/desligar, parâmetros (confiança mínima, RSI mínimo, retorno/risco), timeframe; link "testar no Backtest" | Cada parâmetro com tooltip e faixa; mudar parâmetro sugere rodar backtest; marcar "vale só para novas operações" |
| **Risco** | Risco por operação (0,5%, teto 1%), perda diária (1%, teto 5%), drawdown (5%, fixo), meta diária (1%), posições (máx. 2), perdas seguidas (máx. 2) e pausa do par (≥ 24 h), exposição total (50%, teto 80%), por moeda (50%), fração por posição (25%), retorno/risco mín. (1:2), spread máx., volume mínimo, margem de eventos (30 min), taxas | **Aviso quando AUMENTA risco** (faixa âmbar com "antes/agora/custo por perda" e segundo clique); reduzir salva direto; valores acima do teto rígido são recusados com a regra; botão "Voltar ao padrão seguro"; tudo é auditado (quem, quando, antes/depois). Mudanças de risco no Live exigem 2FA |
| **IA** | Opções de assistência de IA (explicar decisões, resumo diário) e seus limites; chave do provedor (mascarada); "A IA não altera ordens nem limites" (decisão do cliente D7) | Deixar claro o que a IA pode e NÃO pode fazer; sem chave: estado vazio com "Conectar" |
| **Conta/Chaves** | Dados da conta, tema, fuso e idioma; 2FA (regerar códigos de recuperação); chaves da Binance (mascaradas, testar, trocar, remover; permissões detectadas; sem saque); sessões ativas; trilha de auditoria | Mostrar só os 4 últimos caracteres; remover chave com robô ligado exige parar antes (explicado); no Live, trocar chave exige 2FA |

- **Estados:** carregando (skeleton do formulário); erro de validação (por campo, em tempo real); erro de salvar (toast + valores mantidos); desconectado (formulário somente leitura com aviso); robô pausado/HALTED (formulário editável, mas "Retomar" é no Controle, com lembrete); FAILED_PERSISTENCE (salvar desabilitado: "O banco está indisponível; suas mudanças não seriam guardadas").
- **Tablet/celular:** abas viram acordeão; botões ≥ 44 px; teclado numérico nos campos de porcentagem.

---

## 11. Riscos de UX (resumo; detalhes no relatório)

- **R1 Botão Parar tudo:** acionado por engano (cliques acidentais, celular) ou, ao contrário, ausente/lento numa emergência. Mitigação: sempre visível, diálogo curto com opção segura padrão ("só bloquear"), sem 2FA, nome/motivo só obrigatórios para vender tudo, sem atalho de teclado, alvo grande mas afastado de ações frequentes.
- **R2 Confusão Paper x Live (e vermelho de LIVE x perda):** mitigação por redundância (cor, forma, palavra, moldura, rótulos "(simulado)"), LIVE cheio e perda só em texto com ▼, totais separados por modo, e testes de usabilidade com a pergunta "isto é dinheiro de verdade?" em ≤ 2 s.
- **R3 Complacência no Paper:** muito verde e confete levam o usuário a ligar o Live cedo. Mitigação: trava com critérios, linguagem sóbria, avisos de amostra, selo de inelegibilidade.
- **R4 Alarmes demais ou de menos:** atenção só para o que exige ação (estado e travas); ruído de decisões "Não entrou" mantido neutro.
- **R5 Dados velhos mostrados como atuais** (WebSocket caiu): mitigação por faixa de desconexão, horário do último dado e esmaecimento.
- **R6 Fricção do 2FA e da frase digitada** pode irritar: aceitável só no Live; Parar tudo e reduzir risco ficam livres.
- **R7 Tooltips no celular** dependem de toque: usar popover acionável e glossário acessível no menu.

## 12. Decisões que dependem do cliente (referenciadas acima)

| Id | Decisão | Opções | Recomendação |
|---|---|---|---|
| D1 | Cor do LIVE | (a) vermelho cheio (pedido) (b) magenta/carmim distinto da perda | (a) com bloco cheio + moldura, perda só em texto com ▼; revisar após teste com usuários |
| D2 | Tema padrão | escuro / claro / seguir o sistema | Escuro como padrão, com alternância em um clique e memória |
| D3 | Parar tudo no celular | igual ao desktop / com segurar 1 s | Igual ao desktop (diálogo de confirmação já protege; segurar atrasa a emergência) |
| D4 | Primeira ativação do Live | risco total / risco reduzido por 7 dias | Reduzido por 7 dias (ex.: 0,25% por operação) |
| D5 | Papéis | um único admin / admin + leitor | Admin + leitor (leitor não vê chaves nem liga/para) |
| D6 | Parar tudo sem conexão do painel | desabilitar / enfileirar comando | Enfileirar com aviso claro (o runner aceita comandos por arquivo, e o `emergency-stop` só reduz risco); exige API da Fase 4 |
| D7 | Papel da IA | só explicar / também sugerir parâmetros | Só explicar e resumir; nunca alterar ordens ou limites |
| D8 | Frase de liberação do Live | "OPERAR COM DINHEIRO REAL" / personalizada | Frase fixa, sem colar |
| D9 | Notificações | só no painel / e-mail/Telegram para HALTED, FAILED_PERSISTENCE, stops cegos prolongados | Painel + um canal externo para os 3 alertas críticos |
