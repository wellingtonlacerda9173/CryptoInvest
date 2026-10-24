export interface RBin {
  from: number;
  count: number;
}

export const WIDTH = 0.5;

/** Agrupa os R das operacoes em faixas de 0,5 R (a faixa que contem o zero fica separada em perda/ganho). */
export function binR(values: number[]): RBin[] {
  if (values.length === 0) return [];
  const lo = Math.floor(Math.min(...values) / WIDTH) * WIDTH;
  const hi = Math.floor(Math.max(...values) / WIDTH) * WIDTH;
  const bins: RBin[] = [];
  for (let from = lo; from <= hi + 1e-9; from += WIDTH)
    bins.push({ from: Math.round(from * 10) / 10, count: 0 });
  for (const v of values) {
    const bin = bins[Math.min(bins.length - 1, Math.floor((v - lo) / WIDTH + 1e-9))];
    if (bin) bin.count += 1;
  }
  return bins;
}
