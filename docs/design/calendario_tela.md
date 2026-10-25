# Tela "Calendário" — especificação de UX/UI

Status: especificação + protótipo estático (`calendario_prototipo.html`). Nenhum código de produção. Estados e textos abaixo são o contrato para o engenheiro.

## 1. Objetivo e público
Responder, para um leigo, em 5 segundos: (1) o robô está liberado ou travado por causa do calendário? (2) está parado agora por causa de algum evento? (3) o que fazer, se algo? Tela somente leitura; a atualização é automática.

## 2. Posição no menu
Rota `/calendario`, rótulo **Calendário**, ícone lucide `CalendarClock`, em `nav.ts` entre **Mercado** e **Decisões ao vivo** (o calendário explica por que o robô não entrou, então fica ao lado de Decisões). No celular entra em `MORE_ITEMS` (menu "Mais"), logo após `/mercado`. Sem badge fixo, exceto: ponto de atenção (âmbar) quando estado = expirando e (vermelho) quando vencido, **sempre com texto acessível** ("Calendário: atenção" / "Calendário: vencido") via `aria-label`/`sr-only`.

## 3. Hierarquia (de cima para baixo)
1. **Título** "Calendário de eventos" + subtítulo "O robô fica parado perto de notícias que sacodem o mercado. Aqui você vê quando."
2. **Selo de estado** (componente `Hero` existente, tons `ok` / `warn` / `bad`; ícone + título + frase + ação). `role="status"`. É o único elemento grande e colorido da tela.
3. **Card "parado agora?"** (janela ativa). Independente do selo: o calendário pode estar válido e o robô pausado por uma janela.
4. **Fatos em 3 colunas** (empilham no celular): Vale até / Última atualização automática / Seu fuso.
5. **Próximos eventos** (lista cronológica, sem gráfico).
6. **Rodapé explicativo** recolhível: "Por que o robô precisa deste calendário?".

Densidade: informação visível sem cliques; o único recolhível é o rodapé explicativo.

## 4. Selo de estado (Hero)
Estado derivado NO CLIENTE a partir de `state`, `valid_until` e do relógio do servidor (`now` + deslocamento, relido a cada 1 s): `expired` se o backend diz `expired`, se não há `valid_until` ou se o relógio passou de `valid_until` (mesmo com erro de rede mantendo dados antigos); `expiring` (<14 dias); `attention` (calendário válido mas janela ativa agora, ou última carga falhou e mostramos dados antigos); `ok` só com dados frescos e sem janela ativa. Erro ou carregando nunca mostram o selo verde. A tela só conhece o calendário (não o kill-switch nem halt): por isso o título é "Calendário em dia", nunca "Robô liberado". Nunca só cor: ícone diferente (CircleCheck, TriangleAlert, CircleX) + título distinto.

| Estado | Tom | Título | Texto | Ação |
|---|---|---|---|---|
| ok | ok | Calendário em dia | Calendário válido por mais {N} dias. Nenhuma ação sua é necessária. | — |
| expirando | warn | Atenção: calendário vence em {N} dias | (N=1) "Atenção: calendário vence amanhã" / (N=0) "Atenção: calendário vence hoje". Texto: Depois disso o robô para de abrir operações. As atuais continuam protegidas. A atualização é automática; se não resolver até lá, avise o suporte. | Link "Ver como pedir ajuda" (se existir canal; senão omitir) |
| atenção (janela ativa) | warn | Calendário em dia, com evento em andamento | O calendário está válido, mas há um evento em andamento. O robô volta a operar quando a pausa terminar. | — |
| atenção (erro de carga com dados antigos) | warn | Não consegui confirmar o calendário agora | Mostro o último estado conhecido: válido por mais {N} dias. Vou tentar de novo sozinho. | — |
| vencido | bad | Calendário vencido | Sem calendário válido o robô não abre novas operações. As posições abertas continuam com stop. A atualização é automática e já falhou; chame o suporte. | Botão "Ver como pedir ajuda" |

Regras de copy: singular/plural ("1 dia"/"N dias"); nunca "FOMC", "CPI" etc. sem explicação em linguagem comum na primeira menção; nunca mostrar o código `event_calendar_stale`.
Se a última tentativa foi "sem mudança" e o calendário está vencido, o texto do vencido troca "já falhou" por "não encontrou calendário novo" (o backend deve distinguir; ver §10).

## 5. Card "O robô está parado agora?"
Calculado no cliente com `now` ∈ [`block_start`, `block_end`] (inclusivo nas duas pontas, igual ao gate): a API entrega cada evento com a pausa EFETIVA (janela menos/mais as margens `event_margin_*_minutes` em uso pelo robô) e o front usa só isso, com o `now` do servidor para não depender do relógio do usuário. "Volta a operar às" usa `block_end`.
- Janela ativa: ícone Pause, borda esquerda âmbar. Título "O robô está parado agora". Texto "Por causa de “{evento}”. Volta a operar às {HH:MM} (horário de Brasília)." Atualiza a cada 1 s (`setInterval`, sem `aria-live` agressivo: `aria-live="polite"` só ao mudar de estado).
- Sem janela ativa e calendário ok: ícone Play, borda verde. "Sem evento por perto" / "Ele opera normalmente. Próxima pausa: {evento}, em {N} dias."
- Calendário vencido: "O robô está parado agora" / "Por segurança, até o calendário voltar."
Coerência: com janela ativa o selo vira atenção (não fica verde ao lado do card "parado agora"). Vencido por relógio: "Venceu hoje" / "Venceu há N dias"; sem `valid_until` vencido (estado expirado com data futura): "Sem calendário válido".

## 6. Lista de próximos eventos
- Ordem cronológica crescente, máx. 10 itens (resto atrás de "Ver mais eventos"). Eventos já encerrados não aparecem.
- Cada item (`<li>` em `<ol>`): 
  - Linha 1: **dia + faixa de horário local**, ex. "sex., 06 de nov., 09:30 às 11:30"; à direita o chip do tipo (texto, nunca só cor).
  - Linha 2: nome em negrito ("Emprego nos EUA (payroll)").
  - Linha 3: explicação de uma frase (tabela abaixo).
  - Linha 4: "Em {N} dias · Fonte: {fonte}" e, à direita, o UTC em detalhe pequeno: "2026-11-06 12:30–14:30 UTC".
- Item com janela ativa: borda 2px âmbar, fundo `warn-bg`, chip "Robô parado agora" com ícone, "Em andamento".
- Relativo: "Hoje", "Amanhã", "Em N dias"; passado nunca.
- Horário no fuso do navegador (`Intl.DateTimeFormat`, `pt-BR`), com o nome do fuso no fato "Seu fuso" ("Brasília (UTC−3)"). UTC sempre em `text-muted` 12px. Se o evento cruzar a meia-noite local, mostrar "09:30 de sex. às 01:00 de sáb.".

| Tipo | Chip | Explicação (frase exata) |
|---|---|---|
| FOMC | Juros EUA | Os EUA anunciam os juros. Todo o mercado se mexe de uma vez. |
| CPI | Inflação EUA | Número da inflação nos EUA. Pode virar o preço em segundos. |
| Payroll | Emprego EUA | Relatório de empregos nos EUA. Costuma causar saltos bruscos. |
| Vencimento Deribit | Opções | Dia em que muitos contratos vencem. Os preços podem oscilar sem aviso. |
| Tipo desconhecido | Evento | Notícia ou data que costuma agitar o mercado. |

O "porquê o robô para" fica no subtítulo da seção ("Em cada um, o robô para antes e só volta depois da janela.") e no rodapé recolhível: "Em horários de notícias fortes o preço dá saltos que passam por cima do stop. Por isso o robô não abre operações perto deles. Sem calendário em dia, ele não sabe quando as notícias saem, então prefere não operar." Termo "stop" usa `GlossaryTerm` existente. Adicionar ao glossário: `calendario` ("Lista de datas em que o mercado costuma ter saltos bruscos. O robô só opera com ela em dia.") e `janela-bloqueio` ("Período em torno de um evento em que o robô não abre operações novas.").

## 7. Fatos
- **Calendário vale até**: "13 de dezembro de 2026" + "{N} dias restantes" (ou "Venceu há {N} dias" em vermelho com ícone).
- **Última atualização automática**: título conforme resultado: ok → "Atualizado hoje às 04:00" (data relativa: hoje/ontem/dd de mês); sem mudança → "Verificado hoje às 04:00" + "Sem mudanças: o calendário já estava em dia."; falhou → "Tentativa hoje às 04:00 falhou" + "Nova tentativa automática em breve." (não prometer horário que o backend não fornece) + em vencido "Última atualização bem-sucedida: {data}". Falha usa ícone TriangleAlert + texto, não só vermelho. Nunca expor mensagem de exceção. Sem tentativa registrada: "Ainda não houve tentativa automática."
- **Seu fuso**: "Brasília (UTC−3)" + "Horários convertidos para você".
Rótulo dos fatos 12px muted, valor 16px semibold (mesmo padrão dos KpiCards).

## 8. Estados da tela
- **Carregando**: esqueleto do selo (altura fixa 96px, sem texto de estado), 3 fatos e 3 itens; `aria-busy="true"`. Nunca mostrar "vencido" ou "vazio" enquanto carrega.
- **Vazio** (calendário sem eventos futuros): selo conforme `state` (provavelmente vencido) + na lista: "**Nenhum evento para mostrar.** Sem calendário válido o robô não opera. Assim que ele for atualizado, os eventos aparecem aqui." (`EmptyState`). Se state=ok e lista vazia (anormal): "Nenhum evento nos próximos 90 dias." 
- **Erro de carga** (`CardError` padrão): "Não consegui carregar o calendário." / "O robô continua seguindo as regras de segurança. Tente de novo." + botão "Tentar de novo". Selo não é exibido (não inventar estado).
- **Dado antigo** (refetch falhou, existe cache): manter dados e faixa discreta "Mostrando dados de {HH:MM}. Sem conexão no momento."
- Polling: refetch a cada 60 s e ao focar a aba (dado muda raramente; evitar flicker).

## 9. Acessibilidade
- Contraste: usar apenas tokens do tema (`gain/warn/loss` sobre `*-bg`, já calibrados nos dois temas; validado visualmente no protótipo: texto principal `--text` sobre `warn-bg` e `gain-bg`; texto muted só em informação secundária 13–14px, que usa `--muted` #a3afbf/#475467, ≥4,5:1 sobre `surface`). Não usar `muted` sobre `warn-bg` em texto relevante.
- Estado nunca só por cor: ícone + título + chip textual.
- Semântica: `h1` título, `h2` no selo e em "Próximos eventos"; selo `role="status"`; lista `<ol>`; cada horário em `<time datetime="ISO UTC">` com texto local; UTC com `aria-label="às 12:30 a 14:30, horário UTC"`.
- Foco: anel de 3px `--info`, ordem natural (selo → ação → lista → rodapé); recolhível via `<details>`/botão com `aria-expanded`, alvo ≥44px.
- Leitor de tela: ícones decorativos `aria-hidden`; o selo lê "Calendário vencido. Sem calendário válido o robô não abre..."; item ativo lê "Robô parado agora" antes do nome.
- `prefers-reduced-motion`: sem animação (a tela não tem nenhuma além de skeleton estático).

## 10. Responsividade
- ≥1024px: coluna única max-w ~880px (mesmo padrão de Mercado/Desempenho), fatos em 3 colunas.
- 640–1023: fatos em 2 colunas.
- <640 (390px): tudo em 1 coluna; selo com ícone 44px; linha 1 do evento quebra: data/hora em cima, chip embaixo; UTC passa para a linha própria alinhada à esquerda; botões 100% de largura, ≥44px. Sem rolagem horizontal; textos longos quebram (`min-w-0`, `break-words`).
- Observação: o screenshot mobile do protótipo foi feito com Edge headless que não reduz a janela abaixo de ~500px e cortou o lado direito; não prova o layout de 390px. Verificar no app real (ver §13).

## 11. Integração com onboarding e Decisões
- **Decisões ao vivo**: o motivo `event_calendar_stale` hoje vira "Não entrou: sem calendário válido". Novo texto: "Não entrou: calendário de eventos vencido" + link inline "Ver calendário" (`/calendario`, foco visível, `aria-label="Ver o calendário de eventos"`). O motivo de janela ativa (se existir código próprio) vira "Não entrou: evento de mercado em andamento (FOMC)" + o mesmo link. Tooltip/glossário: `GlossaryTerm calendario`.
- **Onboarding / critérios Live (`LiveCriteria`)**: linha "Calendário de eventos em dia" com ícone ok/atenção/travado e link "Ver calendário". Estado expirando é aviso (não bloqueia o onboarding); vencido bloqueia com texto "Calendário vencido: o robô não opera até ele ser atualizado".
- **Dashboard/Controle (`StateHero`)**: quando o robô não opera por calendário vencido, o `Hero` ganha ação "Ver calendário" em vez de texto técnico. Expirando <14 dias: faixa `warn` discreta no Dashboard com o mesmo link. Sem faixa quando ok.
- `IneligibleSeal` ("calendário aberto") é conceito diferente (run de backtest); não reutilizar o nome "calendário aberto" ao usuário final para evitar confusão. Sugerir ao engenheiro revisar esse texto para "sem calendário válido no período".

## 12. Contrato de dados exigido do backend (para a UI ficar fiel)
Além do que foi listado: `now` do servidor (para janela ativa e dias restantes sem depender do relógio local); `last_update.at`, `last_update.result` ∈ ok/failed/unchanged; `last_success_at` (para o texto do vencido); `kind` com enum fechado + fallback; campo de fonte textual. Nenhum texto de erro cru deve chegar ao cliente. Adicionar zod em `api/calendarSchemas.ts` e query `useCalendar` com `staleTime` 30 s, no padrão de `marketQueries.ts`; mock em MSW com os três estados (ok/expirando/vencido) para os testes e o `KitPage`.

## 13. Componentes e implementação sugerida (para o engenheiro)
Reusar `Hero`, `KpiCard`-style para fatos, `EmptyState`, `CardError`, `Chip`, `GlossaryTerm`. Novos: `CalendarEventItem`, `RobotPausedCard`. Code-behind do projeto: lógica em hook (`useActiveWindow`), marcação no componente. Testes: 3 estados do selo, janela ativa, fim exclusivo, vazio, erro, plural/singular, fuso (mockar `Intl` com TZ fixo).

## 14. Evidências do protótipo (OBSOLETAS: os PNGs `calendario_*.png` desta pasta foram gerados antes da mudança do título para "Calendário em dia" e mostram "Robô liberado para operar"; o HTML foi atualizado, os PNGs não. Valem os screenshots do app real em `app-screenshots/calendario-*.png`)
Original: renderizadas com Edge headless, tokens reais de `web/src/styles/index.css`.
- `C:\Projetos\CryptoInvest\docs\design\calendario_escuro_ok.png` (escuro, 1280, liberado + janela ativa)
- `C:\Projetos\CryptoInvest\docs\design\calendario_claro_atencao.png` (claro, 1280, expirando)
- `C:\Projetos\CryptoInvest\docs\design\calendario_escuro_vencido.png` (escuro, 1280, vencido + vazio)
- `C:\Projetos\CryptoInvest\docs\design\calendario_mobile_ok.png` (390 pedido; ver ressalva em §10)
Limitações do protótipo: dados fictícios; no estado "expirando" a data "vale até" e "46/9 dias" não foram sincronizadas (erro do protótipo, não da spec); sem foco por teclado, leitor de tela ou contraste medido por script; não usa os componentes React reais.
