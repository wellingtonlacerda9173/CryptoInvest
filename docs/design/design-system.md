# CryptoInvest - Sistema de design do painel (proposta para aprovação)

Status: PROPOSTA. Nenhum código da aplicação foi escrito (`web/` segue vazio). Protótipo estático em `docs/design/mockup/`.
Stack alvo: React + TypeScript + Vite + Tailwind CSS + shadcn/ui, TanStack Query + WebSocket, Lightweight Charts (candles) e Recharts (desempenho). Idioma: pt-BR.

## 1. Princípios

1. **Entender o robô em menos de 3 segundos.** A barra superior fixa responde, sem rolar nem clicar: "está operando?", "é de verdade ou simulado?", "quanto tenho?", "como foi o dia?", "como paro tudo?".
2. **Ficar parado é uma decisão válida.** O robô só opera quando há vantagem. A interface nunca trata "sem operações" como falha: usa tom neutro/positivo, explica o porquê e nunca pressiona o usuário a "fazer algo".
3. **Simulado e real nunca se confundem.** Três sinais redundantes (cor + forma da borda + palavra), moldura da janela em LIVE e rótulo "(simulado)" em todo valor monetário do Paper.
4. **Nunca só cor.** Ganho/perda/atenção/info sempre vêm com ícone (▲ ▼ ⚠ ⓘ ✓ ✕) ou palavra. Vale para daltônicos e para o modo de alto brilho.
5. **Risco explicado em português simples.** Cada termo técnico (drawdown, profit factor, R, ATR, payoff, Sharpe, OCO) tem tooltip com uma frase curta e um exemplo. Cada rejeição do robô tem código técnico (opcional, discreto) e frase humana.
6. **Ações perigosas são difíceis; ações seguras são fáceis.** "Parar tudo" está sempre a um clique e exige uma confirmação curta. Ligar o Live exige critérios cumpridos, frase digitada e 2FA. Parar nunca depende de 2FA nem de digitar frase.
7. **Honestidade dos números.** Resultados de backtest e de Paper trazem o rótulo do modo, premissas (taxas, deslizamento) e avisos de amostra pequena. Nada de verde festivo para lucro simulado.
8. **Densidade operacional.** É uma tela usada o dia inteiro: informação visível, sem esconder dados atrás de cliques, sem animação decorativa.

## 2. Cores

### 2.1 Significado fixo (não muda entre telas nem entre temas)

| Significado | Uso | Ícone/texto obrigatório | Nunca usar para |
|---|---|---|---|
| Verde (`gain`) | Ganho, saudável, conectado, critério cumprido | ▲ / ✓ / "Folgado" | Decoração, botões de ação |
| Vermelho (`loss`) | Perda, erro, desligado, falhou | ▼ / ✕ / palavra | Botões neutros |
| Âmbar (`warn`) | Atenção, pausado, perto do limite, aumento de risco | ⚠ / ‖ / palavra | Erros |
| Azul (`info`) | Informação, Paper, link, foco | ⓘ / palavra | Ganho |
| Cinza (`neutral`) | Parado, desligado por escolha, indisponível | ● / palavra | Alertas |
| Roxo (`backtest`) | Somente o modo BACKTEST | relógio + palavra | Qualquer outra coisa |

Aviso de conflito conhecido: LIVE é vermelho (pedido do cliente) e perda também é vermelho. Mitigação: LIVE é SEMPRE bloco cheio com texto branco, escudo e moldura; perda é SEMPRE texto/ícone ▼ sem preenchimento. Ver risco R2 no fim de `wireframes.md`.

### 2.2 Tokens (tema ESCURO padrão e CLARO)

Troca em um clique (botão na barra superior e nas configurações; respeita `prefers-color-scheme` na primeira visita e lembra a escolha). Tokens como variáveis CSS (`--bg`, ...) mapeadas em `tailwind.config` e nas variáveis do shadcn/ui.

| Token | Escuro | Claro | Uso |
|---|---|---|---|
| `bg` | `#0B0F14` | `#F3F5F8` | Fundo da página |
| `surface` | `#121923` | `#FFFFFF` | Cards |
| `surface-2` | `#1A2330` | `#EEF1F5` | Cards elevados, linhas ao passar o mouse, trilhas |
| `border` | `#2B3747` | `#C5CDD8` | Divisórias decorativas |
| `border-strong` | `#6B7C93` | `#76859A` | Bordas de campos e controles (3:1) |
| `text` | `#E8EDF3` | `#0F172A` | Texto principal |
| `muted` | `#A3AFBF` | `#475467` | Texto secundário |
| `gain` / `gain-bg` | `#4ADE80` / `#0F2A1C` | `#046C3F` / `#E3F6EA` | Ganho |
| `loss` / `loss-bg` | `#FF7B7B` / `#33161A` | `#B42318` / `#FDE8E7` | Perda, erro |
| `warn` / `warn-bg` | `#FBBF24` / `#33270B` | `#8A4B00` / `#FFF1D6` | Atenção, pausa |
| `info` / `info-bg` | `#7DB4FF` / `#11233F` | `#1D4ED8` / `#E6EEFF` | Informação, Paper |
| `neutral` | `#9AA6B5` | `#475467` | Parado/desligado |
| `backtest` / `backtest-bg` | `#C4B5FD` / `#231C3D` | `#5B35B8` / `#EFE9FD` | Modo Backtest |
| `live-fill` / `live-text` | `#B91C1C` / `#FFFFFF` | `#B91C1C` / `#FFFFFF` | Selo LIVE, botão Parar tudo |
| `primary` / `primary-text` | `#2563EB` / `#FFFFFF` | `#1D4ED8` / `#FFFFFF` | Botão primário |
| `focus` | `#7DB4FF` | `#1D4ED8` | Anel de foco (3px + 2px de recuo) |

Observação: o âmbar do tema claro é um marrom-âmbar escuro (`#8A4B00`) de propósito; um âmbar "vivo" não passa em contraste sobre branco.

### 2.3 Contraste WCAG AA verificado

Razões calculadas pela fórmula WCAG 2.1 (luminância relativa sRGB) sobre os valores efetivos dos tokens acima, por script (Python), não estimadas. Texto: mínimo 4,5:1. Elementos gráficos e bordas de controle: mínimo 3:1. Resultado: **50 de 50 pares aprovados**.

#### Tema escuro
| Par | Frente | Fundo | Razão | Mínimo | Resultado |
|---|---|---|---|---|---|
| Texto principal / fundo | `#E8EDF3` | `#0B0F14` | 16.33:1 | 4.5:1 | OK |
| Texto principal / card | `#E8EDF3` | `#121923` | 15.00:1 | 4.5:1 | OK |
| Texto secundário / card | `#A3AFBF` | `#121923` | 7.94:1 | 4.5:1 | OK |
| Texto secundário / fundo | `#A3AFBF` | `#0B0F14` | 8.64:1 | 4.5:1 | OK |
| Texto secundário / card elevado | `#A3AFBF` | `#1A2330` | 7.11:1 | 4.5:1 | OK |
| Ganho (texto) / card | `#4ADE80` | `#121923` | 10.14:1 | 4.5:1 | OK |
| Perda (texto) / card | `#FF7B7B` | `#121923` | 7.04:1 | 4.5:1 | OK |
| Atenção (texto) / card | `#FBBF24` | `#121923` | 10.58:1 | 4.5:1 | OK |
| Informação (texto) / card | `#7DB4FF` | `#121923` | 8.28:1 | 4.5:1 | OK |
| Ganho / fundo da página | `#4ADE80` | `#0B0F14` | 11.03:1 | 4.5:1 | OK |
| Perda / fundo da página | `#FF7B7B` | `#0B0F14` | 7.66:1 | 4.5:1 | OK |
| Ganho / faixa de ganho | `#4ADE80` | `#0F2A1C` | 8.81:1 | 4.5:1 | OK |
| Perda / faixa de perda | `#FF7B7B` | `#33161A` | 6.59:1 | 4.5:1 | OK |
| Atenção / faixa de atenção | `#FBBF24` | `#33270B` | 8.76:1 | 4.5:1 | OK |
| Informação / faixa de info | `#7DB4FF` | `#11233F` | 7.37:1 | 4.5:1 | OK |
| Selo BACKTEST (texto) / card | `#C4B5FD` | `#121923` | 9.57:1 | 4.5:1 | OK |
| Selo LIVE e botão Parar tudo (texto) / fundo | `#FFFFFF` | `#B91C1C` | 6.47:1 | 4.5:1 | OK |
| Botão primário (texto) / fundo | `#FFFFFF` | `#2563EB` | 5.17:1 | 4.5:1 | OK |
| Status cinza (texto) / card | `#9AA6B5` | `#121923` | 7.15:1 | 4.5:1 | OK |
| Borda de campo/controle / card | `#6B7C93` | `#121923` | 4.15:1 | 3.0:1 | OK |
| Borda de campo/controle / fundo | `#6B7C93` | `#0B0F14` | 4.51:1 | 3.0:1 | OK |
| Borda de controle / card elevado | `#6B7C93` | `#1A2330` | 3.71:1 | 3.0:1 | OK |
| Anel de foco / card | `#7DB4FF` | `#121923` | 8.28:1 | 3.0:1 | OK |
| Anel de foco / fundo | `#7DB4FF` | `#0B0F14` | 9.01:1 | 3.0:1 | OK |
| Âmbar (ícone) / fundo | `#FBBF24` | `#0B0F14` | 11.51:1 | 3.0:1 | OK |

#### Tema claro
| Par | Frente | Fundo | Razão | Mínimo | Resultado |
|---|---|---|---|---|---|
| Texto principal / fundo | `#0F172A` | `#F3F5F8` | 16.35:1 | 4.5:1 | OK |
| Texto principal / card | `#0F172A` | `#FFFFFF` | 17.85:1 | 4.5:1 | OK |
| Texto secundário / card | `#475467` | `#FFFFFF` | 7.69:1 | 4.5:1 | OK |
| Texto secundário / fundo | `#475467` | `#F3F5F8` | 7.04:1 | 4.5:1 | OK |
| Texto secundário / card elevado | `#475467` | `#EEF1F5` | 6.79:1 | 4.5:1 | OK |
| Ganho (texto) / card | `#046C3F` | `#FFFFFF` | 6.52:1 | 4.5:1 | OK |
| Perda (texto) / card | `#B42318` | `#FFFFFF` | 6.57:1 | 4.5:1 | OK |
| Atenção (texto) / card | `#8A4B00` | `#FFFFFF` | 6.80:1 | 4.5:1 | OK |
| Informação (texto) / card | `#1D4ED8` | `#FFFFFF` | 6.70:1 | 4.5:1 | OK |
| Ganho / fundo da página | `#046C3F` | `#F3F5F8` | 5.97:1 | 4.5:1 | OK |
| Perda / fundo da página | `#B42318` | `#F3F5F8` | 6.02:1 | 4.5:1 | OK |
| Ganho / faixa de ganho | `#046C3F` | `#E3F6EA` | 5.78:1 | 4.5:1 | OK |
| Perda / faixa de perda | `#B42318` | `#FDE8E7` | 5.60:1 | 4.5:1 | OK |
| Atenção / faixa de atenção | `#8A4B00` | `#FFF1D6` | 6.09:1 | 4.5:1 | OK |
| Informação / faixa de info | `#1D4ED8` | `#E6EEFF` | 5.76:1 | 4.5:1 | OK |
| Selo BACKTEST (texto) / card | `#5B35B8` | `#FFFFFF` | 7.94:1 | 4.5:1 | OK |
| Selo LIVE e botão Parar tudo (texto) / fundo | `#FFFFFF` | `#B91C1C` | 6.47:1 | 4.5:1 | OK |
| Botão primário (texto) / fundo | `#FFFFFF` | `#1D4ED8` | 6.70:1 | 4.5:1 | OK |
| Status cinza (texto) / card | `#475467` | `#FFFFFF` | 7.69:1 | 4.5:1 | OK |
| Borda de campo/controle / card | `#76859A` | `#FFFFFF` | 3.76:1 | 3.0:1 | OK |
| Borda de campo/controle / fundo | `#76859A` | `#F3F5F8` | 3.44:1 | 3.0:1 | OK |
| Borda de controle / card elevado | `#76859A` | `#EEF1F5` | 3.31:1 | 3.0:1 | OK |
| Anel de foco / card | `#1D4ED8` | `#FFFFFF` | 6.70:1 | 3.0:1 | OK |
| Anel de foco / fundo | `#1D4ED8` | `#F3F5F8` | 6.14:1 | 3.0:1 | OK |
| Âmbar (ícone) / fundo | `#8A4B00` | `#F3F5F8` | 6.23:1 | 3.0:1 | OK |

Notas: bordas decorativas de card (`border`, 1,5:1) são intencionalmente discretas e NÃO carregam informação; todo controle interativo usa `border-strong`. As barras de limite têm trilha com `border-strong` e o preenchimento é acompanhado de palavra ("Folgado/Atenção/No limite"). O botão "Parar tudo" (6,47:1) e o selo LIVE passam com texto branco. Os valores serão revalidados por um teste automático (axe + script de contraste) quando a aplicação for escrita.

## 3. Tipografia

- Família: **Inter** (auto-hospedada, WOFF2 variável, `font-display: swap`; sem CDN para não vazar IP/depender de rede). Fallback: `"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif`. O protótipo usa o fallback porque Inter não está instalada na máquina de revisão.
- **Números em fonte tabular** (`font-variant-numeric: tabular-nums`; classe Tailwind `tabular-nums`) em TODO valor que muda: saldo, P&L, preços, porcentagens, relógio, tabelas. Evita que os dígitos "pulem" quando atualizam por WebSocket. Alinhamento à direita em colunas numéricas.
- Escala (px / peso): Título de página 24/700 · Valor KPI 28/700 (22 no celular) · Valor da barra superior 17/700 · Título de card 15/700 · Corpo 15/400 · Secundário 13/400 · Legenda/selo 12/700 com `letter-spacing .06em` nos selos de modo. Máximo de 6 tamanhos e 3 pesos (400/600/700).
- Formato brasileiro: `1.234,56`; porcentagem `0,3%`; moeda `USDT` como unidade em cinza menor ao lado do valor; datas `20/10 14:00` (fuso exibido no rodapé e nas configurações; travas diárias zeram às 00:00 UTC e a UI diz "UTC" onde importa). Sinal negativo com "−" (U+2212), nunca hífen.

## 4. Espaçamento, raios, sombras, elevação

- Espaçamento base 4px: `1=4, 2=8, 3=12, 4=16, 5=20, 6=24, 8=32, 12=48`. Cards: padding 20 (16 no celular); espaço entre cards 16; entre seções 20.
- Raios: controle 10, selo 8, card 16 ("cards arredondados"), pílula 999 (status, chips). Diálogos 16.
- Elevação (3 níveis): 0 = página (`bg`); 1 = card (`surface` + sombra 1px suave + borda); 2 = popover/tooltip/toast/diálogo (`surface` + sombra `0 8px 24px` + borda forte). Sem sombras coloridas, exceto o halo discreto do selo LIVE.
- Sombras suaves: claro `0 1px 2px rgb(16 24 40 / .08)`; escuro `0 1px 2px rgb(0 0 0 / .35)` (no escuro a separação vem mais da borda do que da sombra).

## 5. Grid e breakpoints

| Faixa | Largura | Layout |
|---|---|---|
| Desktop | ≥ 1101 px (alvo 1280-1920; testado 1440) | Menu lateral 248 px (recolhível), conteúdo até 1280 px, 12 colunas lógicas; KPIs em 4 colunas; cards em 2 colunas (≈ 60/40) |
| Tablet | 641-1100 px (testado 820) | Menu vira trilho de ícones 72 px (com tooltip); KPIs 2 colunas; cards em 1 coluna; barra superior quebra em 2 linhas |
| Celular | ≤ 640 px (testado 390) | Sem menu lateral; barra inferior com 5 itens (Início, Controle, Operações, Desempenho, Mais); barra superior em 2 linhas: linha 1 status + modo + Parar tudo, linha 2 saldo e resultado do dia; KPIs 2 colunas; tabelas com rolagem horizontal e coluna Par fixa |

Tailwind: `sm 640`, `lg 1100` customizado (padrão do Tailwind é 1024; a proposta usa 1100 para o trilho de ícones aparecer antes do conteúdo apertar). Um único `max-w-[1280px]` no conteúdo; sem rolagem horizontal da página em nenhuma largura (verificado no protótipo: scrollWidth = largura em 1440/820/390).

## 6. Biblioteca de componentes (shadcn/ui) e variações

Base shadcn/ui (Radix: foco, teclado e ARIA corretos de fábrica) com tokens acima. Ícones: lucide-react (traço 2px, 20px; 16px em selos).

| Componente | shadcn/ui base | Variações e regras |
|---|---|---|
| **Card KPI** | `Card` | Rótulo (13 muted, com tooltip se for termo técnico) + valor 28 tabular + delta com ícone ▲/▼ e texto ("+1,84% desde o início"). Variantes: neutro, ganho, perda, atenção. Skeleton do mesmo tamanho enquanto carrega. Todo valor do Paper leva "(simulado)" no rótulo. |
| **Badge de modo** | `Badge` custom | `BACKTEST` roxo, borda pontilhada, ícone relógio, "histórico". `PAPER` azul, borda tracejada, ícone moeda, "simulado". `LIVE` vermelho PREENCHIDO, borda sólida, ícone escudo, "dinheiro real", halo; com LIVE a janela inteira ganha moldura vermelha de 3 px e faixa de 6 px no topo. Largura fixa na barra superior, nunca some no celular (só encurta o subtexto). |
| **Indicador de status do robô** | `Badge` + animação | Pílula com ponto: verde pulsante = RUNNING; âmbar pulsante rápido = PAUSED/stops cegos; vermelho fixo = HALTED/FAILED_PERSISTENCE; cinza fixo = STOPPED/STARTING/STOPPING. Sempre palavra + subtexto do motivo ("perda diária atingida até 00:00 UTC"). `role="status"`, `aria-live="polite"` (só muda quando o estado muda). O pulso é desligado com `prefers-reduced-motion`; a palavra e a cor continuam. |
| **Botão "Parar tudo"** | `Button` + `AlertDialog` | Vermelho cheio, texto branco, ícone ■, SEMPRE na barra superior (fixa), em todas as telas e larguras; sem 2FA. Abre `AlertDialog`: modo em destaque ("PAPER simulado" ou "LIVE dinheiro real"), 2 opções (só bloquear novas entradas, recomendado / bloquear e vender tudo a mercado), nome do operador, motivo, botão "Confirmar: parar tudo" (vermelho) e "Voltar (Esc)". Foco inicial no campo "Motivo"; Enter confirma só com os campos obrigatórios preenchidos. Em emergência real, o motivo pode ficar em branco SOMENTE se escolher "só bloquear" (ação que só reduz risco); a venda a mercado exige nome e motivo (como o `emergency-stop --liquidate` do runner). Atalho: nenhum (evita acionamento acidental). |
| **Barra de limite** | `Progress` custom | Nome + valor "0,3% de 1%" + trilha com borda forte + preenchimento + palavra de estado: até 60% "Folgado" (verde ✓), 60-90% "Atenção" (âmbar ⚠), ≥ 90% "No limite" (vermelho ✕). Linha de nota diz o que acontece no limite ("se chegar a 1%, o robô para de abrir operações até 00:00 UTC"). `role="progressbar"` com `aria-valuenow`. |
| **Toggle Ligar/Desligar** | `Switch` | 52×30 (alvo ≥ 44 px com o rótulo), texto "Robô ligado/desligado" ao lado + explicação curta. Desligar abre confirmação leve. Ligar no Live NÃO é um toggle: é o fluxo da Trava do Live. Quando HALTED, o toggle some e dá lugar a "Reativar (nome e motivo)". |
| **Toast** | `Sonner` | 3 tipos (sucesso, atenção, erro) com ícone + título + frase. Sucesso some em 5 s; atenção/erro ficam até fechar. `role="status"` (sucesso) / `role="alert"` (erro). Máx. 3 empilhados. Nunca confirma ação perigosa; nunca é o único aviso de erro. |
| **Skeleton** | `Skeleton` | Mesmo tamanho do conteúdo final (sem salto de layout), brilho de 1,4 s; sem brilho em `prefers-reduced-motion`. O estado "vazio" nunca aparece enquanto carrega. |
| **Estado vazio** | custom | Ícone + título + 1 frase + ação sugerida (botão). Exemplo: "Nenhuma operação ainda. O robô só opera quando surge uma boa oportunidade. Ficar parado é normal. [Ver decisões ao vivo]". |
| **Tooltip de termo técnico** | `Tooltip` + `Popover` no toque | Termo com sublinhado pontilhado, focável (Tab) e abre também em foco e toque (no celular abre como popover). Texto de até 2 frases, linguagem simples (glossário, seção 9). Ícone ⓘ só quando o termo não é palavra do texto. |
| **Tabela** | `Table` + TanStack Table | Cabeçalho 12 muted, linhas 44 px, números à direita e tabulares, hover `surface-2`, ordenação por teclado, cabeçalho fixo, coluna Par fixa no celular, rodapé com totais. Linha em atenção ganha ícone, não só cor. |
| **Timeline expansível** | `Collapsible`/`details` | Eventos de controle e auditoria (início, pausa, retomada, reativação, parada, HALT) com data, ícone/cor da borda do marcador, resumo e detalhe expansível (operador, motivo, hash do comando). Teclado: Enter/Espaço expande. |
| **Formulário** | `Form` (react-hook-form + zod) | Validação em tempo real ao sair do campo e a cada tecla depois do primeiro erro; erro junto ao campo com ícone e instrução ("O máximo permitido é 5%. Digite 5 ou menos."); limites vindos do backend (tetos rígidos do risco). Botão Salvar desabilitado com motivo visível. **Aumento de risco:** se o novo valor deixa o robô mais arriscado (ex.: subir risco por operação, subir perda diária, subir exposição), aparece faixa âmbar "Isto aumenta o risco: antes 0,5%, agora 0,9%. Cada perda custará até 90 USDT (hoje: 50)." e o Salvar pede um segundo clique de confirmação. Reduzir risco salva direto. |
| **Diálogo/Drawer** | `AlertDialog`, `Dialog`, `Sheet` | Foco preso, Esc fecha, devolve o foco ao disparador. No celular, `Sheet` de baixo para cima. |
| **Abas** | `Tabs` | Configurações (Pares, Estratégias, Risco, IA, Conta/Chaves). Teclas de seta. |
| **Gráficos** | Lightweight Charts / Recharts | Cores só dos tokens; ganho e perda também por forma (preenchimento vs. contorno, ▲/▼ nos rótulos); resumo textual (`aria-label`) e tabela de dados alternativa. Em celular: menos rótulos nos eixos (o protótipo ainda mostra rótulos pequenos demais no celular: ver pendências). |

## 7. Movimento

| Elemento | Duração | Easing | Regra |
|---|---|---|---|
| Hover/focus de controle | 120 ms | ease-out | cor e borda apenas |
| Toggle, abas, expandir | 180 ms | `cubic-bezier(.2,.8,.2,1)` | deslocamento ≤ 8 px |
| Diálogo/toast entra | 200 ms | ease-out | opacidade + 8 px |
| Barra de limite | 400 ms | ease-out | só ao mudar valor |
| Pulso do status | 2 s (RUNNING), 1,4 s (PAUSED), infinito | ease-out | só 2 estados; é o ÚNICO movimento contínuo |
| Atualização de preço/valor | 0 ms (troca) + realce de fundo 600 ms | linear | sem contagem animada (confunde números) |

Com `prefers-reduced-motion: reduce`: todas as animações e transições são desligadas (o protótipo já faz isso via CSS global); o estado continua claro por palavra e cor. Nada pisca mais de 3 vezes por segundo.

## 8. Acessibilidade

- Foco visível: anel de 3 px `focus` com 2 px de recuo em TODO controle (≥ 3:1 sobre `bg` e `surface`, tabela acima). Nunca `outline: none` sem substituto.
- Teclado: ordem lógica; "Pular para o conteúdo" como primeiro foco; menu com setas; diálogos com foco preso; Esc fecha; Enter envia formulários; tooltips abrem com foco.
- Alvos de toque ≥ 44×44 px (botões, itens do menu 44, toggle com rótulo, abas, linhas de tabela clicáveis, barra inferior 52).
- ARIA: `role="status"` no estado do robô (`aria-live="polite"`); `role="alert"` apenas em bloqueios críticos (HALTED, FAILED_PERSISTENCE, inelegível) e erros; `progressbar` com valores; ícones decorativos `aria-hidden`, ícones com significado têm texto ao lado; gráficos com `role="img"` e resumo; tabelas com `<th scope>`; `aria-current="page"` no menu; `lang="pt-BR"`.
- Leitores de tela: a frase da barra superior é lida inteira ("Operando, aguardando sinal. Modo Paper, simulado. Saldo 10.184,32 USDT. Resultado do dia menos 30,56, menos 0,30 por cento."). Variações de preço em tempo real NÃO são anunciadas (só mudanças de estado e alertas).
- Cor: nunca só cor; contraste da seção 2.3; suporte a zoom 200% e texto 200% sem perder função; `forced-colors` (alto contraste do Windows) mantém bordas e ícones.
- Proteção contra erro humano: sem ação destrutiva por atalho de teclado; confirmações descrevem o efeito ("vende BTC/USDT a mercado agora").

## 9. Microcopy e glossário (tooltips prontos)

Tom: frases curtas, voz ativa, sem culpar ("Não foi possível gravar. Nada foi perdido." e não "Erro do usuário"), sem jargão sem explicação, sem códigos HTTP nem nomes de exceção. Códigos de rejeição do robô aparecem pequenos em fonte mono apenas na tela Decisões/Operações ("event_window").

| Termo | Tooltip |
|---|---|
| Drawdown | Quanto o saldo caiu desde o ponto mais alto. Se o saldo chegou a 10.000 e hoje está em 9.700, o drawdown é de 3%. Se chegar a 5%, o robô desliga sozinho. |
| Profit factor | Quanto o robô ganhou para cada 1 que perdeu. 1,5 significa ganhar 1,50 a cada 1,00 perdido. Abaixo de 1 perde dinheiro. |
| R | O risco de uma operação. Se você arrisca 50 USDT, +1 R são 50 USDT ganhos e −1 R são 50 USDT perdidos. Mede resultado sem depender do tamanho da conta. |
| ATR | Quanto o preço costuma andar por vela. O robô usa para afastar o stop do "ruído" normal do mercado. |
| Payoff | Ganho médio dividido pela perda média. Payoff 2 = os ganhos são, em média, o dobro das perdas. |
| Sharpe | Retorno em relação à oscilação do saldo. Quanto maior, mais regular o resultado. Abaixo de 1 é fraco; aqui é calculado com retornos diários. |
| OCO | Ordem dupla de proteção: uma de stop (limita a perda) e uma de alvo (realiza o ganho). Quando uma executa, a outra é cancelada. O robô nunca cancela o stop sozinho. |
| Stop | Preço em que a operação é encerrada para limitar a perda. Toda entrada tem stop. |
| Stops cegos | O robô ficou sem receber preços enquanto há posição aberta. Ele pausa novas entradas, tenta reconectar e avisa. O stop segue ativo na corretora. |
| Paper | Teste com dinheiro de mentira, preços reais. Nenhuma ordem é enviada à Binance. |
| Perda diária | Quanto o saldo caiu hoje (zera às 00:00 UTC). Ao chegar no limite, o robô não abre novas operações até a meia-noite. |
| Exposição | Parte do saldo que está comprada em moedas (o resto está em USDT). |
| Janela de evento | Período em torno de uma notícia importante (juros, inflação) em que o robô evita entrar. |
| Calendário aberto | O teste rodou sem o calendário de eventos. Funciona, mas esse teste nunca poderá liberar o Live. |
| Walk-forward | Teste em janelas: aprende em um período e confere no seguinte, sem olhar o futuro. |

Mensagens-chave: "Ficar parado em USDT é uma decisão válida." · "Nenhuma pausa ativa." · "Run inelegível para o Live (calendário aberto)." · "Isto aumenta o risco." · "Nada foi perdido; tentaremos de novo sozinhos."

## 10. Protótipo

`docs/design/mockup/`: `dashboard.html`, `controle.html`, `estados.html` (barra superior em todos os estados + componentes), `mockup.css` (tokens reais), `screenshots/*.png` (18 imagens: 3 páginas × 2 temas × 1440/820/390). HTML+CSS puro, sem rede; um mini-script só alterna o tema (`?tema=claro`). Todos os dados são fictícios e rotulados "EXEMPLO".
