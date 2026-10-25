// Fonte unica dos termos tecnicos (design-system.md secao 9). Todo tooltip/popover le daqui.
export const GLOSSARY = {
  drawdown: {
    term: 'Drawdown',
    text: 'Quanto o saldo caiu desde o ponto mais alto. Se o saldo chegou a 10.000 e hoje está em 9.700, o drawdown é de 3%. Se chegar a 5%, o robô desliga sozinho.',
  },
  'profit-factor': {
    term: 'Profit factor',
    text: 'Quanto o robô ganhou para cada 1 que perdeu. 1,5 significa ganhar 1,50 a cada 1,00 perdido. Abaixo de 1 perde dinheiro.',
  },
  r: {
    term: 'R',
    text: 'O risco de uma operação. Se você arrisca 50 USDT, +1 R são 50 USDT ganhos e −1 R são 50 USDT perdidos. Mede resultado sem depender do tamanho da conta.',
  },
  atr: {
    term: 'ATR',
    text: 'Quanto o preço costuma andar por vela. O robô usa para afastar o stop do "ruído" normal do mercado.',
  },
  payoff: {
    term: 'Payoff',
    text: 'Ganho médio dividido pela perda média. Payoff 2 = os ganhos são, em média, o dobro das perdas.',
  },
  sharpe: {
    term: 'Sharpe',
    text: 'Retorno em relação à oscilação do saldo. Quanto maior, mais regular o resultado. Abaixo de 1 é fraco; aqui é calculado com retornos diários.',
  },
  oco: {
    term: 'OCO',
    text: 'Ordem dupla de proteção: uma de stop (limita a perda) e uma de alvo (realiza o ganho). Quando uma executa, a outra é cancelada. O robô nunca cancela o stop sozinho.',
  },
  stop: {
    term: 'Stop',
    text: 'Preço em que a operação é encerrada para limitar a perda. Toda entrada tem stop.',
  },
  'stops-cegos': {
    term: 'Stops cegos',
    text: 'O robô ficou sem receber preços enquanto há posição aberta. Ele pausa novas entradas, tenta reconectar e avisa. O stop segue ativo na corretora.',
  },
  paper: {
    term: 'Paper',
    text: 'Teste com dinheiro de mentira, preços reais. Nenhuma ordem é enviada à Binance.',
  },
  'perda-diaria': {
    term: 'Perda diária',
    text: 'Quanto o saldo caiu hoje (zera às 00:00 UTC). Ao chegar no limite, o robô não abre novas operações até a meia-noite.',
  },
  exposicao: {
    term: 'Exposição',
    text: 'Parte do saldo que está comprada em moedas (o resto está em USDT).',
  },
  'janela-de-evento': {
    term: 'Janela de evento',
    text: 'Período em torno de uma notícia importante (juros, inflação) em que o robô evita entrar.',
  },
  calendario: {
    term: 'Calendário',
    text: 'Lista de datas em que o mercado costuma ter saltos bruscos. O robô só opera com ela em dia.',
  },
  'janela-bloqueio': {
    term: 'Janela de bloqueio',
    text: 'Período em torno de um evento em que o robô não abre operações novas.',
  },
  'calendario-aberto': {
    term: 'Calendário aberto',
    text: 'O teste rodou sem o calendário de eventos. Funciona, mas esse teste nunca poderá liberar o Live.',
  },
  'walk-forward': {
    term: 'Walk-forward',
    text: 'Teste em janelas: aprende em um período e confere no seguinte, sem olhar o futuro.',
  },
  'taxa-de-acerto': {
    term: 'Taxa de acerto',
    text: 'Quantas operações terminaram no lucro, em porcentagem. Sozinha engana: acertar 40% pode dar lucro se cada ganho for maior que cada perda.',
  },
  'barra-elefante': {
    term: 'Barra elefante',
    text: 'Uma vela muito maior que as vizinhas, com corpo forte. É o sinal que algumas estratégias do robô procuram para entrar.',
  },
  regime: {
    term: 'Regime',
    text: 'O jeito como o mercado está se comportando: em tendência ou lateral. O robô escolhe estratégias conforme o regime.',
  },
  expectativa: {
    term: 'Expectativa',
    text: 'Quanto, em média, cada operação rende em R. Positiva: a estratégia ganha com o tempo. Negativa: perde.',
  },
  sobreajuste: {
    term: 'Sobreajuste',
    text: 'Quando a estratégia foi ajustada tanto ao passado que parece ótima nele, mas não funciona em dados novos. Quanto mais combinações você testa, maior o risco.',
  },
  deslizamento: {
    term: 'Deslizamento',
    text: 'A diferença entre o preço que você esperava e o preço em que a ordem de fato executou. Medido em pontos-base: 5 pontos são 0,05%.',
  },
  latencia: {
    term: 'Latência',
    text: 'Quantas velas o robô demora para agir depois de ver o sinal. Com 1 vela, ele entra só na abertura da vela seguinte.',
  },
  'operacoes-evitadas': {
    term: 'Operações evitadas',
    text: 'Sinais que o robô viu e não operou. Cada um é simulado à parte para mostrar o que teria acontecido. A soma não é o efeito real no saldo.',
  },
  'confianca-minima': {
    term: 'Confiança mínima',
    text: 'Nota de 0 a 1 que a estratégia dá a cada sinal. Só sinais acima do mínimo viram operação: subir o mínimo faz o robô operar menos.',
  },
  'retorno-risco': {
    term: 'Retorno/risco',
    text: 'Quanto o alvo de ganho precisa ser maior que a perda máxima. 2 significa mirar ganhar o dobro do que se arrisca. O mínimo permitido é 2.',
  },
  rsi: {
    term: 'RSI',
    text: 'Indicador de 0 a 100 que mede se o preço subiu rápido demais (alto) ou caiu rápido demais (baixo). As estratégias usam faixas dele para evitar entradas esticadas.',
  },
} as const;

export type GlossaryKey = keyof typeof GLOSSARY;
