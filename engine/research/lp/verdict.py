"""Texto de veredito (pt-BR, leigo) redigido sobre a execucao registrada neste relatorio."""

VERDICT = """## Veredito em linguagem simples

**Pergunta:** depois de gas e de perder valor quando o preco se move, as taxas que um pool paga compensam
mais do que simplesmente guardar as duas moedas?

1. **Guardar 50/50 (HODL) e a regua.** Todos os numeros abaixo sao "quanto a mais (ou a menos) o LP rendeu do que o HODL".
2. **Pool "estilo v2" (liquidez em toda a faixa de preco):** na conta ideal (supondo que o pool seja v2 com o mesmo volume e o mesmo
   dinheiro depositado) ganha do HODL em quase todos os pools, porque a perda por variacao de preco foi pequena frente as taxas no periodo.
   Mas isso e um caso **hipotetico**: numa posicao de faixa total dentro de um pool v3 real, a taxa e dividida com liquidez concentrada
   dos outros; com `kappa` = 5 o ganho mediano cai para cerca de +2% ao ano e com `kappa` = 20 para cerca de zero (secao 7).
   **Conclusao (confianca media): faixa total nao oferece vantagem relevante sobre o HODL.**
3. **v3 com faixa fixa (+-10%):** o preco sai da faixa na maior parte do ano e o capital fica parado numa so moeda. No caso base perde para o HODL
   (mediana negativa, menos da metade dos pools vence). So vence se os outros LPs forem pouco concentrados (rho < 0,33), hipotese otimista.
   **Conclusao (confianca media): nao supera HODL.**
4. **v3 com rebalanceamento:** no caso base (rho = 0,5, definido antes de ver os dados) vence bastante, principalmente em L2 onde o gas e
   desprezivel. Mas o resultado **troca de sinal** dentro da faixa plausivel de rho (equilibrio em rho ~ 0,70); com rho = 0,75 ja perde, com
   rho = 1 perde muito (a "perda de rebalancear" e grande: vende barato e compra caro varias vezes ao ano). Sem dados de liquidez por tick nao da para
   saber de que lado esta a realidade. **Conclusao: inconclusiva, confianca BAIXA; o numero positivo vem de uma hipotese, nao de um dado.**
5. **Estaveis (USDC/USDT etc.) em faixa estreita:** com a hipotese base o retorno parece enorme (dezenas de % ao ano), mas com rho = 1 cai para
   +3-4% ao ano, que e a ordem de grandeza coerente com a realidade; o resultado depende quase so da hipotese de concorrencia. Risco de de-peg:
   no cenario tipo UST a perda adicional por causa do choque foi da ordem de -50% a -70% do capital (versus -24% para faixa total); no cenario tipo
   USDC/mar-2023 (com recuperacao) o impacto foi pequeno, exceto para a versao rebalanceada (cerca de -12% por vender no fundo).
   **Conclusao: ganho pequeno e assimetrico (centavos por muito risco de cauda). Confianca media.**
6. **Gas:** em L2 e Polygon e irrelevante (centesimos de % do capital). Em Ethereum L1, a estrategia rebalanceada custa ~9% de US$ 1.000,
   ~1,9% de US$ 5.000 e ~0,4% de US$ 25.000 por ano: **em L1, carteiras pequenas nao valem a pena**.
7. **Vieses:** sobrevivencia (so existem pools vivos hoje) torna tudo mais **otimista**; a secao 6 separa os pools que perderam mais de 50% do TVL.
   Nao foram modelados JIT/MEV e selecao adversa (tornam o resultado real **pior** que o simulado) nem os ticks reais. Pools de L2 menores
   (TVL de US$ 1-3 mi) tem poucos pontos independentes; moedas medias tem apenas 5 pools (amostra pequena demais para concluir).

### Recomendacao

**Nao ha, com estes dados, oportunidade demonstrada que justifique construir um robo de LP.** A unica regra que parece funcionar
(v3 rebalanceada +-10% em pools grandes de L2, capital >= ~US$ 1.000; em L1 apenas >= ~US$ 25.000) so tem retorno positivo se os demais LPs forem
menos concentrados que rho ~ 0,7, o que nao conseguimos verificar. Antes de qualquer robo seria preciso validar: (a) distribuicao real de
liquidez por tick (subgraph com chave ou leitura on-chain, fora das regras desta pesquisa); (b) custo de JIT/MEV e selecao adversa; (c) volume
on-chain real por dia (aqui implicito pelo apyBase da DefiLlama); (d) teste fora da amostra em outro periodo. Ate la, HODL e a decisao padrao.
"""
