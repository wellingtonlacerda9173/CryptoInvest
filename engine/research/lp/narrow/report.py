from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from engine.research.lp.narrow.aggregate import (
    Pool,
    calibration_summary,
    cash_return,
    decomposition,
    lvr_ratio,
    mean,
    select,
)
from engine.research.lp.stats import median, quantile

KEY_CONFIGS = (
    "0.25%|ao fechar fora",
    "0.5%|ao fechar fora",
    "0.5%|preemptiva 80%",
    "1%|preemptiva 80%",
    "1%|preemptiva 80% + cooldown 6h",
    "2%|preemptiva 80% + cooldown 6h",
    "5%|preemptiva 80% + cooldown 24h",
    "10%|preemptiva 80% + cooldown 24h",
)
WIDTH_LABELS = ("0.25%", "0.5%", "1%", "2%", "5%", "10%")
REBAL_RULES = (
    "ao fechar fora",
    "preemptiva 70%",
    "preemptiva 80%",
    "preemptiva 90%",
    "preemptiva 80% + cooldown 6h",
    "preemptiva 80% + cooldown 24h",
)
NETWORK = {
    "arbitrum": "Arbitrum",
    "base": "Base",
    "optimism": "OP Mainnet",
    "polygon_pos": "Polygon",
    "unichain": "Unichain",
}


def pct(x: float, digits: int = 1) -> str:
    if x != x:
        return "n/d"
    return f"{x * 100:+.{digits}f}%".replace(".", ",")


def pct_plain(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%".replace(".", ",")


def usd(x: float) -> str:
    return f"US$ {x:,.0f}".replace(",", ".")


def num(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".replace(".", ",")


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def key_label(key: str) -> str:
    width, rule = key.split("|")
    return f"±{width} · {rule}".replace(".", ",")


def network_of(pool: Pool) -> str:
    return NETWORK.get(str(pool["pool_id"]).split(":")[0], str(pool["chain"]))


def verdict_numbers(primary: dict[str, Any]) -> dict[str, Any]:
    pools = primary["pools"]
    summary = primary["summary"]
    rebal = [k for k in summary if not k.endswith("|fixa")]
    positive = [k for k in rebal if summary[k]["median"] > 0]
    best = max(rebal, key=lambda k: summary[k]["median"])
    cells = [(p["pool_id"], p["name"], k, p["results"][k]["excess"]) for p in pools for k in rebal]
    pos_cells = sorted((c for c in cells if c[3] > 0), key=lambda c: c[3], reverse=True)
    return {
        "n_rebal": len(rebal),
        "positive_configs": positive,
        "best": best,
        "best_median": summary[best]["median"],
        "best_share": summary[best]["share_pos"],
        "cells": len(cells),
        "pos_cells": pos_cells,
        "share_pos_cells": len(pos_cells) / len(cells),
    }


def _ref_cells(sens: dict[str, Any], key: str) -> list[float]:
    cols = (
        "0.5%|ao fechar fora",
        "1%|preemptiva 80%",
        "1%|preemptiva 80% + cooldown 6h",
        "2%|preemptiva 80% + cooldown 6h",
    )
    return [sens["scenarios"][key]["cells"][c]["median"] for c in cols]


def capital_bullet(sens: dict[str, Any] | None) -> str:
    head = "- **Com que capital?** "
    if sens is None:
        return head + "(sensibilidade não executada)"
    labels = (
        ("capital_100", "US$ 100"),
        ("base", "US$ 1.000"),
        ("capital_5000", "US$ 5.000"),
        ("capital_25000", "US$ 25.000"),
    )
    txt = ", ".join(f"{lab}: {pct(median(_ref_cells(sens, k)), 0)}" for k, lab in labels)
    return (
        head
        + f"{'Em nenhum tamanho testado o resultado ficou positivo' if all(median(_ref_cells(sens, k)) <= 0 for k, _ in labels) else 'Houve tamanho com mediana positiva'} (mediana das 4 combinações de referência, vs HODL: {txt}). "
        "Capital pequeno sofre mais com o gás fixo por giro; capital grande dilui o gás, mas a perda por seleção adversa é proporcional ao capital e "
        "as trocas passam a mover o preço do pool (seção 7)."
    )


def conditions_bullet(sens: dict[str, Any] | None) -> str:
    head = "- **Em quais condições seria positivo?** "
    if sens is None:
        return head + "(sensibilidade não executada)"
    flips = [
        (sc["label"], median(_ref_cells(sens, k)))
        for k, sc in sens["scenarios"].items()
        if k != "base" and median(_ref_cells(sens, k)) > 0
    ]
    if not flips:
        return head + "Em nenhuma das sensibilidades testadas a mediana ficou positiva."
    listing = "; ".join(f"{lab} ({pct(v, 0)})" for lab, v in flips)
    return (
        head
        + f"Só quando se supõe MENOS concorrência nos mesmos preços do que o dado real mostra: {listing}. "
        "É esse o cenário de muitas 'ferramentas de simulação': tratam o dinheiro dos outros LPs como espalhado por todos os preços, "
        "quando na verdade (seção 9) ele já está muito concentrado perto do preço atual (κ de ~5 a ~30), o que dilui várias vezes as taxas "
        "de quem concentra. As sensibilidades são exploratórias e NÃO provam que esse cenário exista."
    )


def section_answer(primary: dict[str, Any], sens: dict[str, Any] | None) -> str:
    v = verdict_numbers(primary)
    pools, meta = primary["pools"], primary["meta"]
    hours = median([p["hours"] for p in pools])
    cash = cash_return(hours, meta["cash_rate_year"])
    btc = select(pools, {"btc"})
    ref = "1%|preemptiva 80% + cooldown 6h"
    d_all, d_btc = decomposition(pools, ref), decomposition(btc, ref)
    pos_pools = defaultdict(list)
    for pid, name, k, _x in v["pos_cells"]:
        pos_pools[f"{name} ({pid.split(':')[0]})"].append(k)
    tiny = ", ".join(sorted(pos_pools)[:6]) or "nenhum"
    lines = [
        "## 1. Resposta direta",
        "",
        f"**Não: nesta amostra, a estratégia (faixa estreita, reajustada antes de sair) NÃO lucra depois de todos os custos "
        f"— e perde para simplesmente guardar as moedas (HODL).** Em {len(v['positive_configs'])} das {v['n_rebal']} combinações "
        f"de largura × regra de reajuste a mediana dos {meta['n_pools']} pools superou o HODL"
        + (f" ({', '.join(key_label(k) for k in v['positive_configs'])})." if v["positive_configs"] else ".")
        + f" A melhor combinação (escolhida DEPOIS de ver os resultados, portanto otimista) teve mediana de {pct(v['best_median'])} "
        f"sobre o HODL em ~{int(hours / 24)} dias ({key_label(v['best'])}), com {pct_plain(v['best_share'], 0)} dos pools acima do HODL.",
        "",
        capital_bullet(sens),
        f"- **Em qual rede?** Nenhuma rede barata mudou o resultado: o gás de L2 é pequeno (cerca de US$ "
        f"{num(d_btc['cost_per_rebalance_usd'], 2)} por giro, mediana nos pools BTC, gás+troca) frente à perda por seleção adversa. "
        f"Só {len(v['pos_cells'])} de {v['cells']} células (pool × combinação) ficaram positivas ({pct_plain(v['share_pos_cells'], 1)}), "
        f"concentradas em pools como: {tiny}. São casos isolados, escolhidos DEPOIS de ver os dados, em pools pequenos onde a posição "
        f"é fatia grande da liquidez e o volume passado pode não se repetir — não é replicável com segurança.",
        conditions_bullet(sens),
        f"- **Contra o dólar rendendo {pct_plain(meta['cash_rate_year'], 1)} ao ano** ({pct(cash)} no período): a posição de "
        f"referência ({key_label(ref)}) rendeu mediana de {pct(d_all['net_return_median'])} nos {meta['n_pools']} pools "
        f"(HODL mediano: {pct(d_all['hodl_return_median'])}).",
        "",
        "Por que as ferramentas de simulação mostram retornos altos? Elas costumam mostrar apenas a primeira linha da tabela da seção 3 "
        f"(as taxas: nesta amostra, mediana de {pct(d_all['fees_median'])} do capital em ~{int(hours / 24)} dias para a posição de referência, "
        "o que anualizado parece enorme) e esquecem as outras: a perda que cada reajuste cristaliza, o gás e a troca de moedas.",
    ]
    return "\n".join(lines)


def section_data(primary: dict[str, Any], dataset: dict[str, Any] | None) -> str:
    pools = primary["pools"]
    rows = [
        [
            p["name"],
            network_of(p),
            {"btc": "BTC/dólar", "eth": "ETH/dólar", "media": "moeda média"}[p["category"]],
            usd(p["tvl_usd"]),
            pct_plain(p["fee"], 2),
            pct_plain(p["sigma_year"], 0),
            pct(p["results"]["1%|fixa"]["hodl_return"]),
        ]
        for p in pools
    ]
    extra = ""
    if dataset is not None:
        extra = (
            f"\nFonte: GeckoTerminal (API pública, sem chave), {dataset['history_hours'] // 24} dias de candles por hora com "
            f"volume real em USD, coletados em {dataset['fetched_at']}. Critério de escolha (fixado antes de rodar): por rede, o pool "
            "Uniswap v3 de maior liquidez para BTC/dólar e ETH/dólar, e o de maior volume para uma moeda média, com pelo menos "
            "190 dias de vida. Pools descartados por pouca cobertura horária: "
            f"{len(dataset['skipped'])}.\n"
        )
    return (
        "## 2. O que foi testado\n"
        + extra
        + "\n"
        + table(
            [
                "Pool",
                "Rede",
                "Tipo",
                "Liquidez (TVL)",
                "Taxa",
                "Volatilidade anual",
                "Retorno do HODL no período",
            ],
            rows,
        )
        + "\n\nObservação: os pools de moeda média e alguns de BTC/ETH em redes pequenas são MUITO pequenos; neles a nossa posição vira "
        "uma fatia grande do pool e as contas são menos confiáveis."
    )


def section_decomposition(primary: dict[str, Any]) -> str:
    pools = primary["pools"]
    parts = ["## 3. De onde vem o ganho e para onde ele vai", ""]
    parts.append(
        "Todos os números são em % do capital inicial, para ~180 dias, **média entre os pools do grupo** (assim as linhas somam). "
        "'Perda por seleção adversa' é a perda que cada reajuste cristaliza: em cada ciclo comparamos o valor da posição com o valor que "
        "teria quem apenas segurasse as moedas do início do ciclo (é a 'perda impermanente' realizada, também chamada LVR). "
        "'Efeito de exposição' é o que sobra por ter ficado com mais/menos BTC que o HODL ao longo do caminho. "
        "Como os custos saem do bolso (não do capital), um total abaixo de -100% significa que os custos sozinhos já passam o capital."
    )
    for title, cats in (("Pools BTC/dólar", {"btc"}), ("Todos os pools", None)):
        group = select(pools, cats)
        rows = []
        for key in KEY_CONFIGS:
            d = decomposition(group, key)
            rows.append(
                [
                    key_label(key),
                    pct(d["fees"]),
                    pct(d["il"]),
                    pct(d["directional"]),
                    pct(d["gas"]),
                    pct(d["swap"]),
                    pct(d["excess"]),
                    f"{pct(d['excess_median'])} ({pct(d['excess_p10'], 0)} a {pct(d['excess_p90'], 0)})",
                    pct_plain(d["share_pos"], 0),
                ]
            )
        parts += [
            f"\n**{title}** ({len(group)} pools)\n",
            table(
                [
                    "Faixa · regra",
                    "Taxas recebidas",
                    "Perda por seleção adversa (IL realizada)",
                    "Efeito de exposição",
                    "Gás",
                    "Custo de troca (taxa + impacto)",
                    "= Resultado vs HODL (média)",
                    "Mediana (P10 a P90)",
                    "% dos pools acima do HODL",
                ],
                rows,
            ),
        ]
    return "\n".join(parts)


def section_turns(primary: dict[str, Any]) -> str:
    pools = primary["pools"]
    btc = select(pools, {"btc"})
    rows = []
    for w in WIDTH_LABELS:
        cells = [w.replace(".", ",")]
        for rule in (
            "ao fechar fora",
            "preemptiva 80%",
            "preemptiva 80% + cooldown 6h",
            "preemptiva 80% + cooldown 24h",
        ):
            d = decomposition(btc, f"{w}|{rule}")
            cells.append(f"{num(d['per_day'])}/dia · US$ {num(d['cost_per_rebalance_usd'], 2)}")
        rows.append(cells)
    return (
        "## 4. Quantos giros por dia e quanto custa cada um\n\n"
        "Mediana nos pools BTC/dólar (capital de US$ 1.000; 'US$' = gás + troca por giro). Uma semana = 7 × o valor diário.\n\n"
        + table(
            [
                "Faixa",
                "Ao fechar fora",
                "Preemptiva 80%",
                "Preemptiva 80% + espera 6h",
                "Preemptiva 80% + espera 24h",
            ],
            rows,
        )
        + "\n\nFaixas muito estreitas giram dezenas de vezes por dia: o preço de BTC/ETH oscila mais que ±0,5% várias vezes por dia."
    )


def fixed_bridge(summary: dict[str, Any]) -> str:
    f = summary["10%|fixa"]
    return (
        "\n\nPonte com o estudo anterior: "
        f"faixa fixa ±10% teve mediana {pct(f['median'])} sobre o HODL (P10 a P90: {pct(f['p10'], 0)} a "
        f"{pct(f['p90'], 0)}; {pct_plain(f['share_pos'], 0)} dos pools acima). O estudo anterior (545 dias, perfil de liquidez suposto) deu resultado "
        "negativo; aqui o período foi lateral para BTC/ETH (preço terminou perto do início) e o perfil de liquidez é o real. Confiança baixa: um único regime."
    )


def section_grid(primary: dict[str, Any]) -> str:
    summary, meta = primary["summary"], primary["meta"]
    rows = []
    for w in WIDTH_LABELS:
        cells = [f"±{w}".replace(".", ",")]
        for rule in ("fixa", *REBAL_RULES):
            s = summary[f"{w}|{rule}"]
            flag = ""
            if s["significant_bonferroni"]:
                flag = " (↓*)" if s["median"] < 0 else " (↑*)"
            cells.append(f"{pct(s['median'], 0)}{flag}")
        rows.append(cells)
    return (
        "## 5. Quadro completo: mediana do resultado vs HODL (% do capital, ~180 dias)\n\n"
        f"{meta['n_pools']} pools; {usd(meta['capital_usd'])} de capital; gás médio. '*' = diferença significativa do 50/50 pelo teste "
        f"binomial já corrigido para {meta['n_tests']} comparações (Bonferroni, alfa = {meta['bonferroni_alpha']:.4f}); "
        "'↓' = significativamente ABAIXO do HODL. 'fixa' = nunca reajusta (sai da faixa e fica parada numa moeda); "
        "NÃO é a estratégia perguntada, só referência.\n\n"
        + table(["Faixa", "Fixa", *REBAL_RULES], rows)
        + fixed_bridge(summary)
        + "\n\nAtenção: a linha 'fixa' mostra mediana positiva só porque a posição fica pouco tempo em faixa, ganha taxas altas "
        "enquanto está nela e depois vira uma moeda só; é um resultado de caminho (depende do preço ter subido ou caído), não uma estratégia contínua."
    )


def section_regimes(primary: dict[str, Any]) -> str:
    summary = primary["summary"]
    rows = []
    for key in (
        "1%|preemptiva 80% + cooldown 6h",
        "5%|preemptiva 80% + cooldown 24h",
        "10%|preemptiva 80% + cooldown 24h",
        "10%|fixa",
    ):
        reg = summary[key]["regimes"]
        cells = [key_label(key)]
        for tag in ("alta", "baixa", "lateral", "vol_alta", "vol_baixa"):
            r = reg.get(tag)
            cells.append(f"{pct(r['median'], 1)} (n={int(r['n'])})" if r else "n/d")
        rows.append(cells)
    return (
        "## 6. Em regimes diferentes de mercado\n\n"
        "Blocos de 30 dias de cada pool (mesma estratégia contínua; excesso sobre o HODL no bloco). Alta/baixa = preço do token variou "
        "mais de ±10% no bloco; vol. alta = volatilidade acima da mediana do próprio pool.\n\n"
        + table(["Faixa · regra", "Alta", "Baixa", "Lateral", "Vol. alta", "Vol. baixa"], rows)
    )


def section_sensitivity(sens: dict[str, Any] | None) -> str:
    if sens is None:
        return "## 7. Sensibilidades\n\n(não executadas)"
    cols = (
        "0.5%|ao fechar fora",
        "1%|preemptiva 80%",
        "1%|preemptiva 80% + cooldown 6h",
        "2%|preemptiva 80% + cooldown 6h",
    )
    rows = []
    for key, sc in sens["scenarios"].items():
        cells = [sc["label"] + (" **(base)**" if key == "base" else "")]
        for c in cols:
            s = sc["cells"][c]
            cells.append(f"{pct(s['median'], 0)} ({pct_plain(s['share_pos'], 0)} acima)")
        rows.append(cells)
    return (
        "## 7. Sensibilidades (rotuladas, exploratórias — rodadas à parte e NÃO usadas para escolher parâmetros)\n\n"
        "Mediana do excesso sobre o HODL (% do capital) e % de pools acima do HODL, variando UM parâmetro por vez.\n\n"
        + table(["Cenário", *(key_label(c) for c in cols)], rows)
    )


def section_lvr(primary: dict[str, Any]) -> str:
    pools = primary["pools"]
    rows = []
    for w in WIDTH_LABELS:
        cells = [f"±{w}".replace(".", ",")]
        for rule in ("ao fechar fora", "preemptiva 80%", "preemptiva 80% + cooldown 6h"):
            cells.append(num(lvr_ratio(pools, f"{w}|{rule}"), 2))
        rows.append(cells)
    return (
        "## 8. Checagem pela teoria (LVR de Milionis et al.)\n\n"
        "Fórmula: perda por seleção adversa ≈ σ²/4 × L × √P por unidade de tempo dentro da faixa (em pool de produto constante, σ²/8 do valor do pool; "
        "em v3 o L é concentrado, então a perda é muito maior por dólar). Tabela: mediana de (perda simulada) / (perda pela fórmula). "
        "Valores perto de 1 indicam que a simulação reproduz a ordem de grandeza da teoria; em passeio aleatório sintético o teste automatizado "
        "exige 0,7 a 1,3.\n\n"
        + table(["Faixa", "Ao fechar fora", "Preemptiva 80%", "Preemptiva 80% + espera 6h"], rows)
        + "\n\nCom faixas estreitas a posição perde valor tão rápido que o capital encolhe e a perda acumulada é um pouco menor que a fórmula "
        "linear (efeito composto). Nos dados reais há também tendência (não é passeio aleatório puro) e a trajetória dentro da hora é uma suposição."
    )


def section_depth(primary: dict[str, Any], cal: dict[str, Any] | None) -> str:
    rows = []
    for p in primary["pools"]:
        d = p["depth"]
        rows.append(
            [p["name"], network_of(p)]
            + [
                f"{usd(w['usd_for_half_share'])} (κ={num(w['kappa'], 1)})"
                for w in d["widths"]
                if w["half_width"] in (0.005, 0.01, 0.05)
            ]
            + [str(d["ticks_read"])]
        )
    text = (
        "## 9. Concentração dos outros LPs (dado real, mas só do momento atual)\n\n"
        "Lido por RPC público (somente leitura: `slot0`, `liquidity()`, `tickBitmap`, `ticks`). Para cada largura: quanto dinheiro seria preciso para ter "
        "**metade** da liquidez daquela faixa, e κ = liquidez média dos outros ÷ a que o TVL teria se todos estivessem em faixa total. "
        "κ alto = os outros já são muito concentrados (a vantagem de concentrar some). É um retrato de agora, NÃO o histórico dos 180 dias.\n\n"
        + table(["Pool", "Rede", "±0,5%", "±1%", "±5%", "Ticks lidos"], rows)
    )
    if cal is not None:
        c = calibration_summary(cal)
        geo = ", ".join(
            f"±{float(k) * 100:g}%: {num(v, 2)}×"
            for k, v in sorted(c["geo"].items(), key=lambda kv: float(kv[0]))
        )
        fails = "; ".join(f"{k}" for k in c["failures"]) or "nenhuma"
        text += (
            "\n\n**Validação com eventos reais (replay de `Swap` por RPC):** em "
            f"{c['n_windows']} janelas de 24 h ({c['n_pools']} pools), o volume somado dos eventos foi {num(c['volume_ratio'], 2)}× o volume do "
            f"GeckoTerminal (1,00 = idêntico). Taxas REAIS de uma posição fixa de US$ 1.000 ÷ taxas do MODELO (média geométrica): {geo}. "
            "Faixas largas (±2% a ±5%) batem bem; faixas muito estreitas (±0,25%, ±0,5%) têm dispersão grande janela a janela (fator 2 a 5 para cima e para "
            f"baixo), porque dependem da ordem do caminho de preço dentro da hora. Pools sem replay (Arbitrum: o RPC público devolve blockTimestamp=0 e não guarda estado antigo; Base: limite de requisições; OP Mainnet: resposta grande demais): {fails}."
        )
    return text


def section_biases() -> str:
    rows = [
        [
            "O resultado é negativo (a estratégia não lucra)",
            "**Média-alta**",
            "Robusto a gás, atraso, capital e competição extra; a perda por seleção adversa bate com a teoria. DEPENDE da profundidade dos outros LPs: ficaria positivo se tivessem entre 1/4 e 1/2 da liquidez atual (seção 7). Amostra de ~180 dias, 14 pools correlacionados.",
        ],
        [
            "A perda por seleção adversa é da ordem de grandeza indicada",
            "**Média**",
            "Confere com a fórmula teórica e com passeio aleatório sintético; a ordem min/máx dentro da hora é suposição.",
        ],
        [
            "Taxas recebidas em faixas ≥ ±2%",
            "**Média**",
            "Replay de eventos reais bate com o modelo (razão ≈ 1).",
        ],
        [
            "Taxas recebidas em faixas ±0,25% a ±0,5%",
            "**Baixa**",
            "Erro de fator 2–5 por janela no replay; o perfil de liquidez dos outros é de um instante só.",
        ],
        [
            "Pools positivos em redes/pools minúsculos",
            "**Baixa**",
            "Posição grande demais para o pool; volume passado pode não se repetir; viés de seleção ex-post.",
        ],
        [
            "Comparação com o dólar a 4,5% a.a.",
            "**Média**",
            "A taxa é uma referência declarada, não medida; os números de gás são premissas.",
        ],
    ]
    return (
        "## 10. Vieses e confiança por conclusão\n\n"
        + table(["Conclusão", "Confiança", "Por quê / principais vieses"], rows)
        + "\n\nVieses declarados: (1) **sobrevivência** (otimista): só pools que existem hoje e foram escolhidos pela liquidez de HOJE; (2) a profundidade dos outros LPs "
        "é de um instante e suposta estacionária; (3) pools do mesmo BTC/ETH são correlacionados, então os p-valores são otimistas; "
        "(4) múltiplas comparações: 42 combinações, Bonferroni aplicado ao teste binomial; (5) a grade foi fixada antes de rodar e não foi ajustada; "
        "(6) janela de ~180 dias (limite da fonte) cobre poucos regimes; (7) sem look-ahead: os reajustes só usam o preço até o instante."
    )


def section_not_measured() -> str:
    return (
        "## 11. O que NÃO foi possível medir\n\n"
        "- Liquidez dos outros LPs **ao longo do tempo** (só há o retrato atual; histórico por tick exigiria RPC de arquivo/indexador com chave).\n"
        "- Taxas reais por posição via `fee_growth` histórico (replay de `Swap` cobre só 3 dias por pool; Mint/Burn não foram reconstruídos).\n"
        "- JIT/MEV reais e o efeito de gestores ativos (só como parcela hipotética de 25%/50%).\n"
        "- Custo real de gás por rede (premissa documentada; o RPC permitido não inclui `eth_gasPrice`).\n"
        "- A ordem real de máxima/mínima dentro de cada hora e o volume por faixa de preço dentro da hora.\n"
        "- Mais de 180 dias, pools que morreram (sobrevivência), outras redes (Linea, zkSync, Scroll não foram sondadas), impostos, risco de contrato/ponte."
    )


def section_recommendation(primary: dict[str, Any]) -> str:
    v = verdict_numbers(primary)
    return (
        "## 12. Recomendação\n\n"
        f"**Não automatizar nem colocar dinheiro relevante nessa estratégia.** Em {len(v['positive_configs'])} de {v['n_rebal']} combinações a mediana "
        "superou o HODL e a perda por reajuste é estrutural (cada giro realiza a perda que a faixa estreita acumula). O ganho de taxas é real, mas fica "
        "abaixo da soma de perda por seleção adversa + gás + troca. Se o objetivo é render sem depender de preço, uma alternativa sem esse risco "
        "(dólar em renda fixa/stablecoin lending, ~4,5% a.a. como referência) é mais honesta; se o objetivo é aprender, use o micro-teste abaixo com "
        "valor que você aceita perder por inteiro. Não existe aqui evidência para ampliar o capital."
    )


PROTOCOL = """## 13. Protocolo OPCIONAL de micro-teste manual (apenas texto; o robô não executa nada)

**Objetivo:** medir com dinheiro mínimo se as taxas reais cobrem a perda por seleção adversa + custos, comparando com HODL. Não é investimento.

**Antes de começar**
1. Use uma **carteira nova e isolada**, criada só para o teste, com valor que você **aceita perder inteiro** (ex.: o equivalente a um jantar). Nunca a carteira principal.
2. **Nunca** cole a frase-semente (seed) ou chave privada em site, chat, planilha ou neste robô. Ninguém legítimo pede isso.
3. Confira o **endereço oficial** do contrato (pelo site oficial do Uniswap e pelo explorador da rede); desconfie de links de anúncios, DMs e "suporte". Digite o endereço, não clique.
4. Cuidado com **aprovações de token**: aprove apenas o valor necessário (não "ilimitado") e revogue depois (revoke.cash ou o explorador).
5. Rede barata (por exemplo Arbitrum ou Base), par BTC (WBTC/cbBTC) ou ETH com dólar de **taxa 0,05%** e boa liquidez. Anote o custo de gás de cada transação.
6. Impostos: no Brasil, trocas e rendimentos podem gerar obrigação (ganho de capital/IN 1888). Guarde todos os comprovantes e consulte um contador.

**Como conduzir (2 a 4 semanas)**
1. Registre **hoje**: valor em dólar depositado (50% moeda volátil, 50% dólar), preço, data, e quanto custou o gás. Faça a conta do HODL: "se eu tivesse só guardado as mesmas moedas, quanto valeria?".
2. Escolha uma faixa (ex.: ±2%) e **não ajuste mais de uma vez por dia** (cada ajuste custa gás + troca + realiza perda).
3. Pelo painel de posições do Revert (revert.finance) ou do próprio Uniswap, anote **todo dia no mesmo horário**: valor atual da posição, taxas acumuladas (não coletadas), preço, se está em faixa.
4. Planilha com colunas: data, valor da posição, taxas acumuladas, gás gasto até agora, valor do HODL. Resultado líquido = valor da posição + taxas − gás; compare com o HODL e com o dólar rendendo ~4,5% a.a.

**Quando encerrar:** ao completar o prazo; ou se a perda líquida passar de 30% do valor do teste; ou se o pool perder liquidez/volume de forma brusca; ou ao notar qualquer comportamento estranho do site/contrato.

**Quando NÃO fazer:** se o valor faz falta, se vai usar a carteira principal, se alguém prometeu retorno, se não entende o que está aprovando, se há mercado em pânico/de-peg de estável, se não tem como registrar tudo, ou se os custos de gás do dia estão altos.

**Avisos de risco:** golpes de token falso e sites clonados; aprovações que esvaziam a carteira; risco de contrato inteligente; perda por seleção adversa mesmo com o preço "voltando"; liquidez que some; custos e impostos reduzem o ganho."""


def build_report(
    primary: dict[str, Any],
    sens: dict[str, Any] | None,
    cal: dict[str, Any] | None,
    dataset: dict[str, Any] | None = None,
) -> str:
    meta = primary["meta"]
    header = (
        "# Faixas estreitas + reajuste antes de sair: lucra depois de TODOS os custos?\n\n"
        f"Gerado em {meta['generated_at']}. **Estudo de análise: sem carteira, chave, assinatura ou transação.** Fontes: GeckoTerminal (API pública, sem chave) "
        "e RPC público somente leitura. Números em % do capital inicial salvo indicação.\n\n"
        "**Pergunta:** e se o robô criar um pool dólar × BTC numa rede barata (L2) com faixa MUITO estreita, ganhar taxas e, ANTES do preço sair da faixa, desfizesse e "
        "criasse outra em nova faixa, mantendo o ganho de taxas sem depender de ganho de preço?"
    )
    sections = [
        header,
        section_answer(primary, sens),
        section_data(primary, dataset),
        section_decomposition(primary),
        section_turns(primary),
        section_grid(primary),
        section_regimes(primary),
        section_sensitivity(sens),
        section_lvr(primary),
        section_depth(primary, cal),
        section_biases(),
        section_not_measured(),
        section_recommendation(primary),
        PROTOCOL,
    ]
    return "\n\n".join(sections) + "\n"


def write_narrow_report(primary_path: Path, sens_path: Path, cal_path: Path, out_path: Path) -> str:
    primary = json.loads(primary_path.read_text(encoding="utf-8"))
    sens = json.loads(sens_path.read_text(encoding="utf-8")) if sens_path.exists() else None
    cal = json.loads(cal_path.read_text(encoding="utf-8")) if cal_path.exists() else None
    dataset_path = Path("data/cache/lp/narrow_dataset.json")
    dataset = None
    if dataset_path.exists():
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        dataset = {k: v for k, v in dataset.items() if k != "pools"} | {"skipped": dataset["skipped"]}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_report(primary, sens, cal, dataset), encoding="utf-8")
    return str(out_path)


__all__ = ["build_report", "mean", "quantile", "write_narrow_report"]
