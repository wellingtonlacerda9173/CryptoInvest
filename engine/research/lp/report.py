from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from statistics import fmean
from typing import Any

from engine.research.lp.stats import (
    binom_two_sided,
    bonferroni_alpha,
    bootstrap_median_ci,
    median,
    quantile,
    share_positive,
)
from engine.research.lp.verdict import VERDICT

Row = dict[str, Any]
CATS = ("volatil_maior", "volatil_media", "estavel")
KINDS = ("v2", "v3_fixed", "v3_rebal")
KIND_NAME = {
    "v2": "LP v2 (faixa total)",
    "v3_fixed": "LP v3 faixa fixa",
    "v3_rebal": "LP v3 com rebalanceamento",
}
CAT_NAME = {
    "volatil_maior": "moedas grandes (ETH/BTC)",
    "volatil_media": "moedas medias",
    "estavel": "estaveis",
}
ALPHA = 0.05


def chain_group(row: Row) -> str:
    return "L1 (Ethereum)" if row["chain"] == "Ethereum" else "L2/Polygon"


def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def per_pool(rows: Iterable[Row], field: str = "excess") -> dict[str, float]:
    acc: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        acc[r["pool_id"]].append(r[field])
    return {k: median(v) for k, v in acc.items()}


def select(rows: Iterable[Row], **conds: Any) -> list[Row]:
    return [r for r in rows if all(r[k] == v for k, v in conds.items())]


def cell(rows: list[Row]) -> dict[str, Any]:
    vals = list(per_pool(rows).values())
    n = len(vals)
    wins = sum(v > 0 for v in vals)
    lo, hi = bootstrap_median_ci(vals)
    return {
        "n_pools": n,
        "mediana": median(vals),
        "p10": quantile(vals, 0.1),
        "p90": quantile(vals, 0.9),
        "pct_pools_acima": share_positive(vals),
        "ic95": [lo, hi],
        "p_binomial": binom_two_sided(wins, n),
    }


def verdict(c: dict[str, Any], alpha_adj: float) -> str:
    if c["n_pools"] < 3:
        return "dados insuficientes"
    if c["mediana"] > 0 and c["ic95"][0] > 0 and c["pct_pools_acima"] > 0.5:
        return "supera HODL" + (
            " (robusto)" if c["p_binomial"] < alpha_adj else " (nao robusto apos correcao)"
        )
    if c["mediana"] < 0 and c["ic95"][1] < 0:
        return "perde para HODL"
    return "inconclusivo"


def load(report_dir: Path, name: str) -> list[Row]:
    path = report_dir / f"results_{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def build(report_dir: Path) -> dict[str, Any]:
    primary, sens, stress = (
        load(report_dir, "primary"),
        load(report_dir, "sensitivity"),
        load(report_dir, "stress"),
    )
    grid = [
        (c, g, k, cap, w)
        for c in CATS
        for g in ("L1 (Ethereum)", "L2/Polygon")
        for k in KINDS
        for cap in (1_000.0, 5_000.0, 25_000.0)
        for w in (90, 180, 365)
    ]
    alpha_adj = bonferroni_alpha(ALPHA, len(grid))
    cells: list[dict[str, Any]] = []
    for c, g, k, cap, w in grid:
        rs = [
            r
            for r in select(primary, category=c, strategy=k, capital=cap, window_len=w)
            if chain_group(r) == g
        ]
        if rs:
            cell_ = cell(rs)
            cell_.update(
                categoria=c, rede=g, estrategia=k, capital=cap, janela=w, veredito=verdict(cell_, alpha_adj)
            )
            cells.append(cell_)
    return {
        "gerado_em": datetime.now(UTC).isoformat(timespec="seconds"),
        "testes_na_grade": len(grid),
        "alpha_corrigido": alpha_adj,
        "celulas": cells,
        "sensibilidade": sensitivity_table(sens),
        "estresse": stress_table(stress),
        "rho": rho_table(primary, sens),
        "decomposicao": decomposition(primary),
        "regimes": regimes(primary),
        "pools_em_queda": decay_split(primary),
        "n_linhas": {"primario": len(primary), "sensibilidade": len(sens), "estresse": len(stress)},
    }


def sensitivity_table(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for label in sorted({r["scenario"] for r in rows}):
        for c in ("volatil_maior", "estavel"):
            for k in KINDS:
                if k == "v2" and "kappa" not in label:
                    continue
                rs = select(rows, scenario=label, category=c, strategy=k, capital=5_000.0, window_len=365)
                if rs:
                    cl = cell(rs)
                    out.append(
                        {
                            "cenario": label,
                            "categoria": c,
                            "estrategia": k,
                            "mediana": cl["mediana"],
                            "pct_pools_acima": cl["pct_pools_acima"],
                            "n_pools": cl["n_pools"],
                        }
                    )
    return out


def rho_table(primary: list[Row], sens: list[Row]) -> list[dict[str, Any]]:
    """Excesso mediano por rho (0 = otimista, 1 = sem vantagem de concentracao) e rho de equilibrio."""
    points = {
        0.0: "rho=0 (ninguem mais concentrado)",
        0.25: "rho=0.25",
        0.5: None,
        0.75: "rho=0.75",
        1.0: "rho=1 (concorrentes igualmente concentrados)",
    }
    out = []
    for c in CATS:
        for k in ("v3_fixed", "v3_rebal"):
            curve: list[tuple[float, float, float]] = []
            for rho, label in points.items():
                rs = select(
                    primary if label is None else sens,
                    category=c,
                    strategy=k,
                    capital=5_000.0,
                    window_len=365,
                )
                if label is not None:
                    rs = [r for r in rs if r["scenario"] == label]
                if rs:
                    cl = cell(rs)
                    curve.append((rho, cl["mediana"], cl["pct_pools_acima"]))
            if len(curve) == len(points):
                out.append(
                    {"categoria": c, "estrategia": k, "curva": curve, "rho_equilibrio": breakeven(curve)}
                )
    return out


def breakeven(curve: list[tuple[float, float, float]]) -> float | None:
    for (r0, m0, _), (r1, m1, _) in pairwise(curve):
        if m0 > 0 >= m1:
            return r0 + (r1 - r0) * m0 / (m0 - m1)
    return None


def stress_table(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    calm = {(r["pool_id"], r["strategy"]): r["excess"] for r in rows if r["scenario"] == "calmo"}
    for sc in sorted({r["scenario"] for r in rows} - {"calmo"}):
        for k in KINDS:
            rs = select(rows, scenario=sc, strategy=k)
            if rs:
                delta = [r["excess"] - calm[(r["pool_id"], k)] for r in rs]
                out.append(
                    {
                        "cenario": sc,
                        "estrategia": k,
                        "mediana": median([r["excess"] for r in rs]),
                        "calmo": median([calm[(r["pool_id"], k)] for r in rs]),
                        "perda_pelo_choque": median(delta),
                        "pior": min(delta),
                        "n": len(rs),
                    }
                )
    return out


def decomposition(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for c in CATS:
        for k in KINDS:
            rs = select(rows, category=c, strategy=k, capital=5_000.0, window_len=365)
            if rs:
                out.append(
                    {
                        "categoria": c,
                        "estrategia": k,
                        "taxas": median([r["fees"] for r in rs]),
                        "efeito_preco": median([r["price_effect"] for r in rs]),
                        "gas": median([r["gas"] for r in rs]),
                        "swap": median([r["swap"] for r in rs]),
                        "rebalanceamentos": median([r["rebalances"] for r in rs]),
                        "tempo_na_faixa": fmean(r["time_in_range"] for r in rs),
                    }
                )
    return out


def gas_table(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for g in ("L1 (Ethereum)", "L2/Polygon"):
        for k in KINDS:
            for cap in (1_000.0, 5_000.0, 25_000.0):
                rs = [r for r in select(rows, strategy=k, capital=cap, window_len=365) if chain_group(r) == g]
                if rs:
                    out.append(
                        {
                            "rede": g,
                            "estrategia": k,
                            "capital": cap,
                            "gas": median([r["gas"] for r in rs]),
                            "excesso": median([r["excess"] for r in rs]),
                            "pct_acima": share_positive([r["excess"] for r in rs]),
                        }
                    )
    return out


def regimes(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for c in CATS:
        for reg in ("alta", "baixa", "lateral", "estavel"):
            for k in KINDS:
                rs = select(rows, category=c, regime=reg, strategy=k, capital=5_000.0, window_len=90)
                if rs:
                    v = [r["excess"] for r in rs]
                    out.append(
                        {
                            "categoria": c,
                            "regime": reg,
                            "estrategia": k,
                            "n_janelas": len(v),
                            "mediana": median(v),
                            "pct_acima": share_positive(v),
                        }
                    )
    return out


def decay_split(rows: list[Row]) -> list[dict[str, Any]]:
    out = []
    for label, falling in (
        ("TVL caiu mais de 50% desde o inicio", True),
        ("TVL estavel ou crescendo", False),
    ):
        for k in KINDS:
            rs = [
                r
                for r in select(rows, strategy=k, capital=5_000.0, window_len=365)
                if (r["tvl_ratio"] < 0.5) is falling
            ]
            if rs:
                out.append(
                    {
                        "grupo": label,
                        "estrategia": k,
                        "n_pools": len({r["pool_id"] for r in rs}),
                        "mediana": median([r["excess"] for r in rs]),
                    }
                )
    return out


def _tbl(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def markdown(data: dict[str, Any], dataset: dict[str, Any] | None, primary: list[Row]) -> str:
    cells = data["celulas"]
    main_rows = [
        [
            CAT_NAME[c["categoria"]],
            c["rede"],
            KIND_NAME[c["estrategia"]],
            str(c["n_pools"]),
            pct(c["mediana"]),
            f"{pct(c['p10'])} a {pct(c['p90'])}",
            f"{c['pct_pools_acima'] * 100:.0f}%",
            f"{pct(c['ic95'][0])} a {pct(c['ic95'][1])}",
            f"{c['p_binomial']:.3g}",
            c["veredito"],
        ]
        for c in cells
        if c["capital"] == 5_000.0 and c["janela"] == 365
    ]
    out = [
        "# Relatorio: fornecer liquidez (LP) vence apenas guardar as moedas (HODL)?",
        f"\nGerado em {data['gerado_em']}. Estudo de analise: sem carteira, chave ou transacao.\n",
        "Numeros em % do capital inicial. 'Excesso' = resultado do LP (apos gas e swaps) menos "
        "o resultado de simplesmente guardar as moedas. Positivo = LP venceu.\n",
        VERDICT,
        "## 1. Resultado principal (janela de 365 dias, posicao de US$ 5.000, regras pre-definidas)\n",
        _tbl(
            [
                "Tipo de pool",
                "Rede",
                "Estrategia",
                "Pools",
                "Mediana",
                "P10 a P90",
                "% pools > HODL",
                "IC95% da mediana",
                "p (binomial)",
                "Veredito",
            ],
            main_rows,
        ),
        f"\nCorrecao para multiplas comparacoes: a grade testada tem {data['testes_na_grade']} celulas; "
        f"limiar de Bonferroni = {data['alpha_corrigido']:.5f}. Os pools nao sao independentes (ETH/BTC), "
        "entao ate os p-valores acima sao otimistas.\n",
        "## 2. Impacto do gas por tamanho de posicao e rede (janela 365d, mediana entre pools)\n",
        _tbl(
            ["Rede", "Estrategia", "Capital", "Gas (% do capital)", "Excesso mediano", "% pools > HODL"],
            [
                [
                    g["rede"],
                    KIND_NAME[g["estrategia"]],
                    f"US$ {g['capital']:,.0f}",
                    pct(g["gas"]),
                    pct(g["excesso"]),
                    f"{g['pct_acima'] * 100:.0f}%",
                ]
                for g in gas_table(primary)
            ],
        ),
        "\n## 3. De onde vem o resultado (365d, US$ 5.000, mediana)\n",
        _tbl(
            [
                "Tipo",
                "Estrategia",
                "Taxas",
                "Efeito de preco (perda impermanente / rebalanceamento)",
                "Gas",
                "Swap",
                "Rebalanc.",
                "Tempo na faixa",
            ],
            [
                [
                    CAT_NAME[d["categoria"]],
                    KIND_NAME[d["estrategia"]],
                    pct(d["taxas"]),
                    pct(d["efeito_preco"]),
                    pct(-d["gas"]),
                    pct(-d["swap"]),
                    f"{d['rebalanceamentos']:.0f}",
                    f"{d['tempo_na_faixa'] * 100:.0f}%",
                ]
                for d in data["decomposicao"]
            ],
        ),
        "\n## 4. Por janela de tempo (US$ 5.000, mediana entre pools)\n",
        _tbl(["Tipo", "Rede", "Estrategia", "90d", "180d", "365d"], window_rows(cells)),
        "\n## 5. Por regime de mercado (90 dias, US$ 5.000; alta/baixa = preco +-20%)\n",
        _tbl(
            ["Tipo", "Regime", "Estrategia", "Janelas", "Mediana", "% janelas > HODL"],
            [
                [
                    CAT_NAME[r["categoria"]],
                    r["regime"],
                    KIND_NAME[r["estrategia"]],
                    str(r["n_janelas"]),
                    pct(r["mediana"]),
                    f"{r['pct_acima'] * 100:.0f}%",
                ]
                for r in data["regimes"]
            ],
        ),
        "\n## 6. Vies de sobrevivencia: pools que perderam TVL (365d, US$ 5.000)\n",
        _tbl(
            ["Grupo", "Estrategia", "Pools", "Mediana"],
            [
                [d["grupo"], KIND_NAME[d["estrategia"]], str(d["n_pools"]), pct(d["mediana"])]
                for d in data["pools_em_queda"]
            ],
        ),
        "\n## 7. ANALISE DE SENSIBILIDADE (separada; nao foi usada para escolher nada)\n",
        _tbl(
            ["Variacao", "Tipo", "Estrategia", "Pools", "Mediana", "% pools > HODL"],
            [
                [
                    s["cenario"],
                    CAT_NAME[s["categoria"]],
                    KIND_NAME[s["estrategia"]],
                    str(s["n_pools"]),
                    pct(s["mediana"]),
                    f"{s['pct_pools_acima'] * 100:.0f}%",
                ]
                for s in data["sensibilidade"]
            ],
        ),
        "\n## 8. Estresse de de-peg em pools de estaveis (cenarios, nao previsoes; 180d, US$ 5.000)\n",
        "Perda causada pelo choque = excesso sob estresse menos excesso do mesmo periodo sem choque.\n",
        _tbl(
            [
                "Cenario",
                "Estrategia",
                "Excesso (estresse)",
                "Excesso (sem choque)",
                "Perda pelo choque (mediana)",
                "Pior pool",
            ],
            [
                [
                    s["cenario"],
                    KIND_NAME[s["estrategia"]],
                    pct(s["mediana"]),
                    pct(s["calmo"]),
                    pct(s["perda_pelo_choque"]),
                    pct(s["pior"]),
                ]
                for s in data["estresse"]
            ],
        ),
        "\n## 8b. Quanto o resultado depende da hipotese de concorrencia (rho), US$ 5.000, 365d\n",
        "rho = 0: ninguem mais concentra liquidez (muito otimista). rho = 1: os outros LPs concentram "
        "tanto quanto eu (sem vantagem de concentracao). Nao ha dados de ticks para saber o valor real.\n",
        _tbl(
            ["Tipo", "Estrategia", "rho=0", "0.25", "0.5 (base)", "0.75", "rho=1", "rho de equilibrio"],
            [
                [
                    CAT_NAME[r["categoria"]],
                    KIND_NAME[r["estrategia"]],
                    *[f"{pct(m)} ({share * 100:.0f}%)" for _, m, share in r["curva"]],
                    "nunca vence" if r["rho_equilibrio"] is None else f"{r['rho_equilibrio']:.2f}",
                ]
                for r in data["rho"]
            ],
        ),
    ]
    if dataset:
        out += [
            "\n## 9. Dados usados\n",
            f"Janela: {dataset['window']['start']} a {dataset['window']['end']}. "
            f"Dataset gerado em {dataset['generated_at']}. {len(dataset['requests'])} GETs.\n",
            _tbl(
                ["Pool", "Rede", "Tipo", "TVL inicio", "TVL fim", "sha256 (dados)"],
                [
                    [
                        p["name"],
                        p["chain"],
                        CAT_NAME[p["category"]],
                        f"{p['tvl_start_usd']:,.0f}",
                        f"{p['tvl_end_usd']:,.0f}",
                        p["sha256"][:16],
                    ]
                    for p in dataset["pools"]
                ],
            ),
            "\nDescartados:\n",
        ]
        out += [f"- {s['chain']} {s['pool_id'][:8]}: {s['motivo']}" for s in dataset["skipped"]]
        digest = sorted({(r["url"], r["fetched_at"], r["sha256"][:12]) for r in dataset["requests"]})
        out += ["\nConsultas (URL, data/hora UTC, hash):\n"] + [f"- {u} | {t} | {h}" for u, t, h in digest]
    return "\n".join(out) + "\n"


def window_rows(cells: list[dict[str, Any]]) -> list[list[str]]:
    idx: dict[tuple[str, str, str], dict[int, float]] = defaultdict(dict)
    for c in cells:
        if c["capital"] == 5_000.0:
            idx[(c["categoria"], c["rede"], c["estrategia"])][c["janela"]] = c["mediana"]
    return [
        [CAT_NAME[k[0]], k[1], KIND_NAME[k[2]], *(pct(v[w]) if w in v else "-" for w in (90, 180, 365))]
        for k, v in idx.items()
    ]


def write_report(report_dir: Path, dataset_path: Path = Path("data/cache/lp/dataset.json")) -> str:
    data = build(report_dir)
    primary = load(report_dir, "primary")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8")) if dataset_path.exists() else None
    md = report_dir / "lp_report.md"
    md.write_text(markdown(data, dataset, primary), encoding="utf-8")
    (report_dir / "lp_report.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return str(md)
