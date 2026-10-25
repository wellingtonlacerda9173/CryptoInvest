# Avaliacao: IA gratuita (local ou plano gratis) como analista do CryptoInvest

Data da analise: 2026-10-25. Escopo: leitura do codigo e da documentacao. Nada foi executado, medido ou
testado com Ollama/llama.cpp; nada em producao foi alterado. O que nao pude conferir esta marcado como
**NAO VERIFICADO**.

## Resumo para quem nao e tecnico

- **Da para usar uma IA gratuita? Sim, tecnicamente.** O robo ja trata a IA como um "segundo par de olhos" com
  poder minimo: ela so pode **vetar** uma entrada ou **baixar a confianca**. Nunca aumenta risco, nunca cria
  ordem, nunca mexe em stop, alvo ou tamanho. Essa trava e do nosso codigo, nao do modelo; vale para qualquer
  modelo, pago ou gratis.
- **Com seguranca?** As travas estruturais se mantem. O que piora com modelo pequeno e a **qualidade**:
  respostas fora do formato (descartadas), opinioes erradas (vetos que perdem bons trades) e lentidao.
  Piora a utilidade, nao a protecao do capital.
- **Treinar a IA (fine-tuning)?** **Nao recomendado agora.** Temos poucos trades, nenhuma prova estatistica de
  que a IA agrega valor e muito risco de o modelo "decorar" o passado. O "treino" seguro e: licoes aprovadas
  por um humano + exemplos no prompt + avaliacao offline.
- **Dinheiro:** o custo atual da IA paga e pequeno (secao 5), entao o ganho de ir para gratuito e mais
  privacidade e independencia do que economia.
- **Recomendacao:** (1) adicionar suporte a provedor compativel com OpenAI (Ollama), (2) rodar um modelo local
  em **modo observacao** (registra o parecer, nao influencia nada), (3) so ligar de verdade se bater criterios
  objetivos medidos (secao 4). Esforco estimado: cerca de 5 a 7 dias uteis de desenvolvimento, mais semanas de
  calendario para juntar amostra.

---

# Secao tecnica

## 1. Acoplamento atual ao provedor

Fluxo hoje: `paper_robot._finish_ai_entry` (apos o gate aprovar) -> `MarketAnalyst.analyze` ->
`AnthropicAnalyst` -> `LlmClient.complete` -> `AnthropicClient` (SDK `anthropic`).

### O que ja e desacoplado
- `engine/infrastructure/ai/client.py` define o Protocol `LlmClient.complete(model, system, user, max_tokens,
  deadline_seconds) -> LlmReply(text, input_tokens, output_tokens)` e a hierarquia `LlmError` /
  `LlmTimeoutError` (com `retryable`). O analista (`analyst.py`) so conhece esse Protocol.
- Todo o resto e agnostico a provedor: parse estrito (`application/ai/parse.py`), `combine`, circuito, limite
  por hora, cache, reserva de orcamento, auditoria (`ledger.py`), licoes, `ai_value`.
- O contrato de dominio `MarketAnalyst` (`application/ai/analyst.py`) isola o runner.

### O que esta acoplado a Anthropic
| Ponto | Arquivo | Detalhe |
|---|---|---|
| Cliente | `infrastructure/ai/client.py` | Unica implementacao e `AnthropicClient`: `output_config={"effort":"low"}` com retentativa sem o parametro, excecoes `anthropic.*` |
| Fabrica | `infrastructure/ai/factory.py` | Exige `settings.anthropic_api_key`; mensagens `NO_KEY`/`NO_PACKAGE`; instancia `AnthropicClient` direto |
| Segredo | `infrastructure/config/settings.py` | `anthropic_api_key` (lista de segredos e scrub de logs) |
| Config | `application/trading_config/document.py` (`AiConfig`) | So `enabled`, `model`, `monthly_budget_usd`, `timeout_seconds`. Nao ha `provider`/`base_url`. `ModelName` aceita `[A-Za-z0-9._:-]` (tags Ollama como `modelo:8b` passam; nomes com `/` estilo Hugging Face nao) |
| Precos | `infrastructure/ai/pricing.py` | Modelo desconhecido cai em US$ 10/50 por 1M tokens (`UNKNOWN_MODEL_PRICE`); `load_prices` recusa preco <= 0 |
| Deploy | `docker-compose.yml` (`ai-api`), `deploy/stack.ai.yml` | Secret `anthropic_api_key`, rede `egress` |
| Dependencia | `pyproject.toml` | `anthropic==2.3.0` (extra `ai`) |
| Nomes | `AnthropicAnalyst`, `AnthropicLessonWriter` | So o nome; a logica usa o Protocol |

### O que seria preciso para aceitar OpenAI/Ollama
1. **Novo cliente** `OpenAiCompatibleClient` implementando `LlmClient` (POST `{base_url}/chat/completions`
   com `httpx`; confirmar se ja e dependencia do projeto). Mapear: timeout -> `LlmTimeoutError`;
   429/5xx/conexao -> `LlmError(retryable=True)`; 4xx -> `retryable=False`; `usage.prompt_tokens/completion_tokens`
   -> `LlmReply` (se o servidor nao devolver `usage`, estimar por `estimate_input_tokens`). Pedir JSON
   (`response_format`/`format` do Ollama): **NAO VERIFICADO** quais versoes/modelos suportam saida estruturada;
   o parse estrito continua sendo a defesa real, entao o pedido de JSON e so otimizacao.
2. **Config:** adicionar a `AiConfig` `provider: Literal["anthropic","openai_compatible"]="anthropic"` e
   `base_url` (validado: so `http(s)`, host em lista permitida/rede interna). **Cuidado:** `ConfigDocument.digest`
   forma a cadeia de versoes; os campos novos precisam ser omitidos do hash quando no padrao, como ja e feito
   para `ai` (ver o docstring do `digest`), senao o hash das versoes existentes muda.
3. **Fabrica:** escolher o cliente por `provider`; chave opcional para locais (Ollama nao exige), obrigatoria
   para APIs hospedadas (novo segredo, ex. `openai_compatible_api_key`).
4. **Precos/orcamento:** modelo local deve custar 0, mas `load_prices` rejeita 0 e o padrao e US$ 10/50. Sem
   ajuste, o orcamento DURO bloquearia chamadas de modelo local ou as cobraria como "premium". Permitir preco 0
   para provedores locais; manter o **limite por hora** (`HourlyLimit`) e o circuito como protecao de carga.
5. **Deploy:** servico `ollama` em rede **interna** (sem `egress`), volume de modelos, e `ai-api`/robo sem
   saida para a internet quando local. `deploy/stack.ai.yml` ganha variante.
6. **Renomear** `AnthropicAnalyst` -> `LlmAnalyst` (opcional, cosmetico; mexe em testes).
7. `MAX_OUTPUT_TOKENS=1024` e timeout padrao 8 s (maximo 15 s, `MAX_AI_TIMEOUT_SECONDS`): em CPU um modelo local
   pode estourar; o teto de 15 s e trava de seguranca e nao deve ser relaxado so por isso.

### Pontos de atencao no fluxo do robo
- A consulta a IA fica **no caminho da entrada** (`_finish_ai_entry` espera o parecer, fora do lock, ate o
  timeout). Latencia alta de modelo local atrasa a entrada; o gate e reavaliado depois do parecer.
- O circuito abre apos 3 falhas seguidas (inclui resposta **invalida**) por 5 min. Modelo pequeno com muita
  saida invalida abriria o circuito com frequencia: seguro (volta a "sem IA"), mas o modelo ficaria inutil.
- O parser exige JSON puro, sem cerca de codigo, sem chave extra/duplicada: modelos pequenos erram isso com
  frequencia; sera preciso medir (secao 4). Nao afrouxar o parser para "ajudar" o modelo.

### Testes necessarios
- Contrato do novo cliente com servidor HTTP falso (`httpx.MockTransport`): sucesso, timeout, 429, 500, 4xx,
  corpo malformado, sem `usage`, resposta vazia, `choices` vazio.
- Reaproveitar `tests/unit/ai/support.py` e rodar `test_analyst`, `test_injection`, `test_contract` tambem com o
  novo cliente (mesmas garantias independentes de provedor).
- Config/hash: versao antiga de config mantem o mesmo digest; `provider` invalido recusado; `base_url`
  malformada ou `file://` recusada.
- Fabrica: provedor local sem chave = ligado; hospedado sem chave = desligado.
- Precos: preco 0 aceito so para provedor local; orcamento nao bloqueia local.
- `tests/deploy/test_local_compose.py`: servico ollama sem rede `egress`.

### Riscos
Saida malformada (descartada; custo e so latencia); SSRF via `base_url` configuravel (validar e restringir);
vazamento de dados se apontar para API hospedada sem querer (o request nao tem saldo/posicoes/identidade, mas
leva candles e sinal); modelo local mal dimensionado travando o host do robo (isolar em container com limites
de CPU/memoria); regressao no hash de config.

### Esforco (estimativa minha, nao medida)
Cliente + fabrica + config + precos: ~1,5 a 2 dias. Testes: ~1 dia. Deploy local isolado: ~0,5 dia. Modo
observacao (secao 4): ~1 a 2 dias. Relatorio de avaliacao: ~1 a 2 dias. **Total: cerca de 5 a 7 dias uteis.**

## 2. Garantias de seguranca

### Mantem-se independentemente do modelo (vem do nosso codigo)
| Garantia | Onde |
|---|---|
| So veta ou reduz: `combine` nunca eleva confianca; aprovacao so faz `min(sinal, ia)`; sem parecer = resultado sem IA | `application/ai/combine.py` |
| IA consultada so apos o gate de risco aprovar, e o gate roda de novo depois (revalidacao) | `paper_robot._finish_ai_entry` |
| Saida estrita: um unico objeto JSON, 3 chaves exatas, tipos estritos, `confidence` 0..1 numerico, razao <= 280, sem NaN/Infinity, sem chave duplicada, sem JSON dentro de texto | `application/ai/parse.py`, `AnalystVerdict` |
| Entrada minima e estrita: sem saldo, posicoes, chaves, identidade; textos sanitizados; `extra="forbid"` | `AnalystRequest`, `sanitize.py` |
| Timeout por chamada (+1 s de margem), 2 tentativas, backoff | `AnthropicAnalyst._call` |
| Orcamento DURO com reserva atomica do pior caso entre processos; sem contador = sem chamada; cancelamento registra custo de pior caso | `analyst._guarded`, `ledger.ReservingStore` |
| Circuito (3 falhas/5 min), limite 30 chamadas/hora, cache por hash | `guards.py` |
| Qualquer falha vira "sem parecer"; excecoes nunca derrubam o runner | `analyze`/`complete` |
| Auditoria append-only (hash do prompt, hash e texto sanitizado da resposta, tokens, latencia, resultado) | `AiCallRecord` |
| Licoes so entram no prompt se a revisao mais recente for `approved` por humano; texto sanitizado, limitado e marcado como nao confiavel; relatorio deterministico como alternativa | `lessons.py` (`lesson_notes`, `deterministic_report`) |
| Valor medido: `ai_value` simula o que cada veto teria rendido; amostra pequena => "sem conclusao" | `application/ai/value.py` |

**Prompt injection (dados de mercado/noticias):** o pedido hoje contem apenas candles numericos, indicadores,
sinal e scores; **nao ha noticias nem texto livre externo** (so nomes de estrategia/confluencias internos).
Mesmo assim os textos vao como JSON em `untrusted_market_data`, passam por `sanitize_text` (que o proprio
codigo declara ser heuristica, nao fronteira) e o prompt de sistema manda ignorar instrucoes nos dados. O
teto real e arquitetural: o pior que um texto injetado consegue e **provocar um veto ou baixar a confianca**
(perda de oportunidade, nao de capital) e escrever uma justificativa, que e sanitizada e exibida escapada. Isto
vale igualmente para modelo pequeno, porem um modelo pequeno tende a **obedecer injecao com mais facilidade**
(afirmacao geral, nao medida aqui), entao o risco de vetos induzidos sobe. Se um dia noticias entrarem no
pedido, repetir esta analise.

### Ficam mais frageis com modelo pequeno/gratuito
- **Saida malformada:** mais respostas descartadas (seguro, mas reduz cobertura e pode abrir o circuito).
- **Alucinacao/raciocinio fraco:** vetos sem base e aprovacoes sem criterio; o sistema nao distingue um veto
  bom de um ruim sem a medicao da secao 4. Veto custa trade perdido; aprovacao nao protege alem do que o gate
  ja garante.
- **Licoes geradas por IA fraca:** `parse_report` exige que a evidencia contenha digitos, **nao** que os
  numeros batam com o resumo. A aprovacao humana vira a unica verificacao real; o revisor deve conferir contra
  o resumo estruturado. Alternativa segura: manter o relatorio deterministico para licoes e usar a IA local so
  para o parecer de entrada.
- **Latencia:** no caminho da entrada; CPU sem GPU pode passar do timeout (8 s padrao, 15 s maximo).
- **Plano gratuito hospedado:** limites de taxa/cota geram 429 (tratado como erro retentavel + circuito).
- **Reprodutibilidade:** o cache por hash ajuda, mas modelo local nao tem a versao fixa de uma API; fixar a tag
  do modelo e registra-la no campo `model` (max 64 caracteres).

## 3. Por que NAO fazer fine-tuning agora

1. **Sem vantagem estatistica comprovada:** o proprio projeto so declara conclusao com `MIN_RESOLVED=10` e
   `CONCLUSIVE_RESOLVED=30` vetos resolvidos (`value.py`); ainda nao sabemos se a IA agrega.
2. **Dados escassos:** um robo spot long-only com poucos pares gera poucas dezenas de trades por mes
   (suposicao; confirmar no banco). Fine-tuning de LLM precisa de ordens de grandeza mais exemplos.
3. **Sobreajuste e vazamento temporal:** o modelo decoraria regimes passados; mercado e nao estacionario.
4. **Rotulos ruins:** "o que teria sido o certo" depende da simulacao pessimista, nao e verdade absoluta.
5. **Custo/risco operacional:** pipeline de treino, versionamento de pesos, auditoria e rollback, tudo novo.
6. **Perda de auditabilidade:** hoje o prompt e versionado (`PROMPT_VERSION`) e legivel; pesos nao sao.

### Alternativa segura: "treino" sem mexer nos pesos
- **Licoes aprovadas por humano** (ja existe): entram como contexto de leitura; so podem deixar o modelo mais cauteloso.
- **Exemplos no prompt (few-shot):** poucos casos reais anonimizados (entrada e resultado), subindo
  `PROMPT_VERSION` a cada mudanca (a versao entra no hash e no ledger). Custo: mais tokens e latencia.
- **Avaliacao offline:** repetir sobre sinais/candles historicos (backtest e `simulate_signal` ja existem) e
  comparar versoes de prompt/modelo com as mesmas metricas antes de qualquer uso.
- Reavaliar fine-tuning so com centenas de vetos resolvidos e vantagem comprovada em observacao.

## 4. Plano de avaliacao

### Modo OBSERVACAO (nao existe hoje; precisa ser implementado)
Hoje, ligar `ai.enabled` ja influencia entradas (`combine`). Proposta: `ai.mode = "observe" | "active"`
(padrao `observe` ao ligar). Em `observe`, o robo consulta a IA (mesma estrada: orcamento, timeout, circuito,
ledger), grava o parecer com `would_veto`/`would_reduce` e **ignora** o resultado na decisao (`combine` nao e
aplicado). Idealmente a consulta nao bloqueia a entrada (feita em paralelo/apos a ordem). Implicacao: `ai_value`
hoje so enxerga vetos que **efetivamente** bloquearam (skip com codigo `ai_veto`); precisa ler tambem os
pareceres de observacao e simular o que cada veto hipotetico teria rendido, comparando com os trades
executados. Ate la, da para cruzar offline o `decision_key` do ledger com os trades.

### Metricas
| Metrica | Definicao |
|---|---|
| Taxa de saida invalida | `invalid` / chamadas respondidas (ledger) |
| Disponibilidade | `ok` / chamadas tentadas (descontando `budget`) |
| Latencia | p50/p95/p99 de `latency_ms` |
| Vetos uteis | vetos cujo trade hipotetico teria dado prejuizo (R < 0) |
| Vetos nocivos | vetos de trades que teriam lucrado (ganho perdido) |
| Efeito liquido | R evitado menos R perdido, com intervalo de confianca (`Z=1.645` como em `value.py`) |
| Concordancia | % de veredito igual entre o modelo candidato e um de referencia (ex. o atual) nos mesmos pedidos. E checagem de sanidade, **nao** prova de acerto |
| Estabilidade | mesmo pedido em 2 execucoes (sem cache): % de veredito igual |
| Cobertura | % de sinais com parecer valido |

### Criterio objetivo para ligar (proposta minha; ajustar antes de adotar)
Ligar `active` (somente com aprovacao humana explicita) se, no periodo de observacao:
1. Pelo menos 30 vetos hipoteticos **resolvidos** (`CONCLUSIVE_RESOLVED`) e veredito do `ai_value` igual a
   `ai_avoided_losses`, com limite inferior do intervalo > 0 (nunca com "amostra insuficiente").
2. Taxa de saida invalida <= 2% e disponibilidade >= 98%.
3. Latencia p95 <= metade do `timeout_seconds` configurado.
4. Teste de injecao (`tests/unit/ai/test_injection.py` e variantes) executado contra o modelo real, sem caso em
   que veredito ou justificativa sigam instrucao presente nos dados.
5. Efeito liquido positivo tambem na segunda metade do periodo (consistencia, nao so o total).
Caso contrario: manter em observacao ou desligar. "Sem conclusao" significa **nao ligar**.
Duracao: depende do volume de sinais, que nao medi; com poucos pares e timeframe de horas, 30 vetos resolvidos
podem levar varias semanas.

## 5. Hardware, limites e privacidade

### Modelos locais (estimativas gerais, NAO medidas neste projeto)
Regra pratica: modelo quantizado em 4 bits usa cerca de 0,5 a 0,7 GB por bilhao de parametros, mais memoria
para o contexto (nosso pedido tem ~24 candles + indicadores: poucos milhares de tokens).
| Classe | RAM/VRAM aproximada | Observacao |
|---|---|---|
| ~3-4B parametros | 3-4 GB | Roda em CPU; JSON e raciocinio tendem a ser fracos |
| ~7-9B | 6-8 GB | Minimo plausivel para instrucao + JSON; GPU de 8 GB, ou CPU com 16 GB de RAM (lento) |
| ~14B | 10-12 GB | Mais consistente; GPU de 12 GB+ recomendada |
| ~30B+ | 20 GB+ | Fora do que faz sentido para este uso |
Qual modelo escolher e a latencia real no nosso hardware: **NAO VERIFICADO** (exige benchmark). Limitar
CPU/memoria do container para nao competir com o robo e o banco.

### Planos gratuitos hospedados
**NAO VERIFICADO:** nao consultei as politicas atuais de nenhum provedor. A checar: limites por minuto/dia,
uso dos dados para treino do provedor, disponibilidade/SLA, mudanca de termos, compatibilidade com o formato
OpenAI. Nosso teto de 30 chamadas/hora e baixo e talvez caiba em cotas gratuitas, mas e suposicao. Este e o pior
caso para nos: os dados saem da maquina e a disponibilidade nao e garantida.

### Custo da alternativa paga (referencia)
Pela tabela do proprio repo (`pricing.py`, copiada da documentacao em 2026-10; **nao reconferi**), o modelo
padrao custa US$ 0,10 entrada e US$ 0,50 saida por 1M tokens. No teto de 30 chamadas/hora x 24 h x 30 dias =
21.600 chamadas/mes; supondo ~2 mil tokens de entrada e ~150 de saida (suposicao), seriam ~43 M de entrada
(~US$ 4,3) e ~3 M de saida (~US$ 1,6): **menos de ~US$ 6/mes no pior caso**, abaixo do orcamento padrao de
US$ 20. A economia de um modelo gratis e pequena; o argumento a favor e privacidade e independencia.

### Privacidade
- O request nao inclui saldo, posicoes, chaves nem identidade (`extra="forbid"`), mas inclui candles, sinal e
  scores, e as licoes aprovadas (que podem revelar desempenho agregado).
- Modelo local em rede sem saida: nada sai da maquina (melhor opcao). API hospedada: dados vao ao provedor.
- Logs: `scrub_text` ja limpa erros; garantir que o novo cliente nunca logue corpo/headers com chave.

## 6. Recomendacao e estimativa

1. **Nao fazer fine-tuning.** Usar licoes aprovadas + few-shot + avaliacao offline.
2. **Implementar o cliente compativel com OpenAI/Ollama** (secao 1), mantendo `anthropic` como padrao e todas as
   travas intactas; preco 0 para local; Ollama em rede interna.
3. **Implementar o modo observacao** e estender `ai_value` para vetos hipoteticos. E pre-requisito para qualquer
   decisao baseada em evidencia, **inclusive para a IA paga atual**, que tambem nao tem prova de valor.
4. **Rodar um modelo local de ~7-9B em observacao** (opcionalmente em paralelo a um modelo de referencia) e
   decidir pelos criterios da secao 4. Manter licoes pelo relatorio deterministico ate o modelo provar
   fidelidade numerica.
5. **Nao usar plano gratuito hospedado** em producao sem conferir os termos (nao verificados aqui).

Estimativa total: **5 a 7 dias uteis de desenvolvimento** + observacao de varias semanas. Se o objetivo for so
reduzir custo, **nao compensa** (poucos dolares por mes). Se for privacidade/independencia, vale pelos itens 2 a 4.
