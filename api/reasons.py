"""Textos em pt-BR simples: motivos de decisão, estados do robô, erros de persistência e glossário.

Tudo que o painel mostra a uma pessoa leiga sai daqui; o código técnico (`RejectionCode`) segue disponível
ao lado do texto para quem precisar procurar no log.
"""

from dataclasses import dataclass
from typing import Final, Literal

from engine.application.risk.rejections import RejectionCode

OutcomeKind = Literal["entered", "waited", "refused", "info"]


@dataclass(frozen=True, slots=True)
class Reason:
    label: str
    text: str
    kind: OutcomeKind


REASONS: Final[dict[str, Reason]] = {
    "approved": Reason(
        "Entrou", "O sinal passou por todos os filtros de risco e a ordem de compra foi enviada.", "entered"
    ),
    "no_signal": Reason(
        "Esperou",
        "Nenhuma estratégia viu uma boa oportunidade nesta vela. Ficar parado em USDT é uma decisão válida.",
        "waited",
    ),
    "catch_up": Reason(
        "Esperou",
        "Vela reprocessada depois de uma queda ou atraso: o robô só cuidou das posições, sem abrir entradas.",
        "waited",
    ),
    "selector_idle": Reason(
        "Esperou",
        "O robô avaliou o mercado e decidiu não operar agora: nenhuma estratégia tem histórico bom o "
        "bastante neste tipo de mercado.",
        "waited",
    ),
    "selector_no_scores": Reason(
        "Esperou",
        "O seletor de estratégias está ligado, mas ainda não há avaliações salvas. O robô fica parado até "
        "elas existirem.",
        "waited",
    ),
    "selector_veto": Reason(
        "Não entrou", "O seletor escolheu outra estratégia (ou nenhuma) para este tipo de mercado.", "refused"
    ),
    "ai_veto": Reason(
        "Não entrou",
        "O assistente de IA vetou esta entrada depois de ela passar pelos filtros de risco. "
        "A IA só pode barrar ou reduzir a confiança, nunca aumentar o risco.",
        "refused",
    ),
    "ai_low_confidence": Reason(
        "Não entrou",
        "O assistente de IA reduziu a confiança do sinal abaixo do mínimo exigido.",
        "refused",
    ),
    "history_gap": Reason(
        "Não entrou", "Faltam dados recentes de mercado para decidir com segurança.", "refused"
    ),
    RejectionCode.ROBOT_HALTED.value: Reason(
        "Não entrou", "O robô está desligado por drawdown e só volta com uma reativação manual.", "refused"
    ),
    RejectionCode.PAUSED.value: Reason(
        "Não entrou", "O robô está pausado: não abre novas operações.", "refused"
    ),
    RejectionCode.DAILY_LOSS_PAUSE.value: Reason(
        "Não entrou", "Limite de perda do dia atingido. O robô volta a operar à meia-noite (UTC).", "refused"
    ),
    RejectionCode.DAILY_TARGET_REACHED.value: Reason(
        "Não entrou", "Meta do dia atingida: o robô parou para proteger o ganho.", "refused"
    ),
    RejectionCode.PAIR_PAUSED.value: Reason(
        "Não entrou", "Par em pausa{pause} depois de perdas seguidas.", "refused"
    ),
    RejectionCode.MAX_POSITIONS.value: Reason(
        "Não entrou", "Já há o máximo de operações abertas ao mesmo tempo.", "refused"
    ),
    RejectionCode.POSITION_ALREADY_OPEN.value: Reason("Não entrou", "Já está comprado neste par.", "refused"),
    RejectionCode.ADDING_TO_LOSER.value: Reason(
        "Não entrou", "O robô não compra mais de uma operação que está no prejuízo.", "refused"
    ),
    RejectionCode.COIN_EXPOSURE.value: Reason("Não entrou", "Já está muito exposto a esta moeda.", "refused"),
    RejectionCode.TOTAL_EXPOSURE.value: Reason(
        "Não entrou", "Já está muito exposto: o total comprado chegou ao limite.", "refused"
    ),
    RejectionCode.SPREAD_TOO_WIDE.value: Reason(
        "Não entrou", "Diferença entre compra e venda grande demais neste momento.", "refused"
    ),
    RejectionCode.LOW_LIQUIDITY.value: Reason("Não entrou", "Pouco volume negociado no par.", "refused"),
    RejectionCode.EVENT_WINDOW.value: Reason(
        "Não entrou", "Perto de uma notícia importante do mercado.", "refused"
    ),
    RejectionCode.HISTORY_GAP.value: Reason(
        "Não entrou", "Faltam dados recentes de mercado para decidir com segurança.", "refused"
    ),
    RejectionCode.REWARD_RISK.value: Reason(
        "Não entrou", "Ganho possível pequeno demais para o risco (mínimo 1 para 2).", "refused"
    ),
    RejectionCode.NET_REWARD_RISK.value: Reason(
        "Não entrou", "Depois das taxas, o ganho possível fica pequeno demais para o risco.", "refused"
    ),
    RejectionCode.INVALID_EQUITY.value: Reason(
        "Não entrou", "Não foi possível conferir o saldo da conta.", "refused"
    ),
    RejectionCode.QUANTITY_BELOW_MIN.value: Reason(
        "Não entrou", "A quantidade calculada é menor que o mínimo aceito pela corretora.", "refused"
    ),
    RejectionCode.NOTIONAL_BELOW_MIN.value: Reason(
        "Não entrou", "O valor da operação ficaria abaixo do mínimo aceito pela corretora.", "refused"
    ),
    RejectionCode.INCONSISTENT_INPUT.value: Reason(
        "Não entrou", "Os dados do sinal não bateram entre si; por segurança o robô não entrou.", "refused"
    ),
    RejectionCode.EVENT_CALENDAR_STALE.value: Reason(
        "Não entrou", "O calendário de eventos está vencido.", "refused"
    ),
    RejectionCode.ENTRY_PENDING.value: Reason(
        "Não entrou", "Já existe uma ordem de compra esperando execução neste par.", "refused"
    ),
    RejectionCode.QUOTE_NOT_ALLOWED.value: Reason(
        "Não entrou", "Este par usa uma moeda de cotação que não está permitida.", "refused"
    ),
    RejectionCode.ASSET_NOT_ALLOWED.value: Reason(
        "Não entrou", "Esta moeda está bloqueada ou fora da lista de permitidas.", "refused"
    ),
    RejectionCode.SIGNAL_STALE.value: Reason("Não entrou", "O sinal ficou velho demais.", "refused"),
    RejectionCode.QUOTE_STALE.value: Reason(
        "Não entrou", "O preço está velho demais para decidir.", "refused"
    ),
    RejectionCode.MARKET_INCOHERENT.value: Reason(
        "Não entrou", "Os preços do mercado vieram incoerentes; o robô não arrisca.", "refused"
    ),
    RejectionCode.PRICE_FILTER.value: Reason(
        "Não entrou", "O preço da ordem não respeita as regras de preço da corretora.", "refused"
    ),
    "stops_blind": Reason(
        "Não entrou", "Sem dados de mercado: novas entradas pausadas. Stops seguem ativos.", "refused"
    ),
    "no_quote": Reason("Não entrou", "Sem cotação fresca para validar a entrada.", "refused"),
    "emergency_exit_pending": Reason("Não entrou", "Uma venda de emergência está em andamento.", "refused"),
    "duplicate_order": Reason("Não entrou", "A ordem já existia; o sinal não foi reenviado.", "refused"),
    "gateway_rejected": Reason("Não entrou", "A corretora simulada recusou a ordem.", "refused"),
    "entry_expired": Reason(
        "Não entrou", "A ordem de compra não executou dentro do prazo e foi cancelada.", "refused"
    ),
    "entry_cancelled_on_restart": Reason(
        "Não entrou", "A ordem de compra foi cancelada porque o robô reiniciou.", "refused"
    ),
    "emergency_stop": Reason("Não entrou", "Parada de emergência: ordem de compra cancelada.", "refused"),
    "shutdown": Reason("Não entrou", "Robô encerrado: ordem de compra cancelada.", "refused"),
    "dust_fill": Reason("Info", "Sobra mínima de execução tratada pelo robô.", "info"),
    "emergency_exit": Reason("Info", "Venda de emergência executada para proteger a conta.", "info"),
}

FALLBACK = Reason("Não entrou", "O robô não entrou nesta vez. O código técnico explica o motivo.", "refused")


def translate(code: str, *, traded: bool = False, pair_pause_hours: int | None = None) -> Reason:
    """`pair_pause_hours`: pausa configurada do par (o texto nunca assume 24 h)."""
    if code in REASONS:
        reason = REASONS[code]
        if "{pause}" in reason.text:
            pause = "" if pair_pause_hours is None else f" de {pair_pause_hours} horas"
            return Reason(reason.label, reason.text.replace("{pause}", pause), reason.kind)
        return reason
    return Reason("Entrou", "Operação aberta.", "entered") if traded else FALLBACK


EXIT_REASONS: Final[dict[str, str]] = {
    "stop_loss": "Stop: o preço caiu até o limite de perda",
    "take_profit": "Alvo: o preço chegou ao ganho planejado",
    "profit_protection": "Proteção de lucro: o robô devolveu pouco e saiu",
    "signal": "Saída por sinal da estratégia",
    "manual": "Saída manual (parada de emergência)",
}

STATES: Final[dict[str, tuple[str, str]]] = {
    "STARTING": ("Iniciando", "O robô está se preparando. Ainda não opera."),
    "RUNNING": ("Operando", "O robô está ligado e esperando uma boa oportunidade."),
    "PAUSED": ("Pausado", "O robô não abre novas operações. Posições abertas seguem protegidas."),
    "FAILED_PERSISTENCE": (
        "Falha ao gravar",
        "O banco de dados está fora. Entradas bloqueadas, stops ativos; nada se perde, a fila grava depois.",
    ),
    "HALTED": (
        "Desligado por drawdown",
        "A queda do saldo chegou ao limite. O robô não volta sozinho: precisa de reativação manual.",
    ),
    "STOPPING": ("Encerrando", "O robô está fazendo uma parada ordenada."),
    "STOPPED": ("Parado", "O robô está desligado. Posições abertas mantêm seus stops."),
    "OFFLINE": ("Sem sinal do robô", "O painel não recebe sinal de vida do robô."),
}


def persistence_text(error: str | None) -> str | None:
    """Erro de banco em linguagem simples (o texto técnico fica só no log do robô)."""
    if not error:
        return None
    return (
        "Não foi possível gravar no banco de dados. O robô tenta de novo sozinho e não perde nenhum evento."
    )


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    term: str
    short: str
    long: str


GLOSSARY: Final[tuple[GlossaryEntry, ...]] = (
    GlossaryEntry(
        "Paper", "Teste com dinheiro de mentira.", "O robô opera com preços reais, mas o saldo é simulado."
    ),
    GlossaryEntry(
        "Live", "Operação com dinheiro real.", "Só pode ser liberado depois de a trava do Live ser cumprida."
    ),
    GlossaryEntry(
        "Backtest", "Teste no passado.", "Simula a estratégia em dados históricos. Não garante o futuro."
    ),
    GlossaryEntry(
        "Walk-forward",
        "Teste em janelas.",
        "Treina em um período e confere em outro que o robô não viu, para evitar enganar-se com sobreajuste.",
    ),
    GlossaryEntry(
        "Stop", "Preço de saída para limitar a perda.", "Se o preço cai até ele, a posição é vendida."
    ),
    GlossaryEntry(
        "Alvo", "Preço de saída para realizar o ganho.", "Se o preço sobe até ele, a posição é vendida."
    ),
    GlossaryEntry(
        "R", "Unidade de risco.", "1 R é o valor arriscado em uma operação. +2 R é ganhar o dobro do risco."
    ),
    GlossaryEntry(
        "Drawdown",
        "Queda do saldo desde o ponto mais alto.",
        "Se o saldo foi a 10.000 e caiu para 9.500, o drawdown é de 5%.",
    ),
    GlossaryEntry(
        "Profit factor", "Ganhos divididos pelas perdas.", "Acima de 1 o robô ganhou mais do que perdeu."
    ),
    GlossaryEntry(
        "Payoff",
        "Ganho médio dividido pela perda média.",
        "Mostra o tamanho do ganho típico frente à perda típica.",
    ),
    GlossaryEntry(
        "Taxa de acerto",
        "Percentual de operações com lucro.",
        "Sozinha não diz se o robô é bom: veja junto o payoff.",
    ),
    GlossaryEntry(
        "Sharpe",
        "Retorno em relação à oscilação.",
        "Calculado com os retornos diários (UTC), anualizado. Quanto maior, mais regular.",
    ),
    GlossaryEntry(
        "Barra elefante",
        "Vela grande e forte.",
        "Uma vela com corpo e volume muito acima do normal, sinal de força do mercado.",
    ),
    GlossaryEntry(
        "EMA", "Média móvel exponencial.", "Linha que acompanha o preço dando mais peso aos preços recentes."
    ),
    GlossaryEntry(
        "RSI", "Índice de força relativa.", "Mede se o preço subiu rápido demais ou caiu rápido demais."
    ),
    GlossaryEntry(
        "Stops cegos",
        "Sem dados de mercado.",
        "Sem preços novos o stop não consegue agir; o robô pausa novas entradas e avisa.",
    ),
    GlossaryEntry(
        "Fila de eventos (outbox)",
        "Eventos esperando para serem gravados.",
        "Quando o banco cai, o robô guarda os eventos em arquivo e grava depois.",
    ),
    GlossaryEntry("Spread", "Diferença entre compra e venda.", "Quanto maior, mais caro é entrar e sair."),
    GlossaryEntry(
        "Exposição",
        "Parte do saldo que está comprada.",
        "Quanto do patrimônio está investido em moedas neste momento.",
    ),
    GlossaryEntry(
        "USDT", "Dólar digital.", "Moeda estável usada como saldo. Ficar em USDT é ficar fora do mercado."
    ),
    GlossaryEntry(
        "Calendário de eventos",
        "Datas de notícias que mexem com o mercado.",
        "O robô evita operar perto delas. Sem calendário, o teste não vale para liberar o Live.",
    ),
    GlossaryEntry(
        "Sobreajuste",
        "Ajustar demais ao passado.",
        "Quando um teste parece ótimo só porque foi calibrado nos mesmos dados.",
    ),
    GlossaryEntry(
        "Operação evitada",
        "Sinal que o robô recusou.",
        "Mostra o que teria acontecido se ele tivesse entrado, para avaliar os filtros.",
    ),
)
