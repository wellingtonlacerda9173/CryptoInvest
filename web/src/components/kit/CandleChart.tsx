import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineStyle,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef } from 'react';
import type { CandleBar, CandleMarker } from '@/api/marketSchemas';
import { cn } from '@/lib/utils';

export interface ChartLevel {
  price: number;
  title: string;
  tone: 'stop' | 'target' | 'entry' | 'exit' | 'blind';
}

interface Props {
  candles: CandleBar[];
  markers?: CandleMarker[];
  levels?: ChartLevel[];
  /** Destaca as barras elefante (contorno + marcador). */
  showElephants?: boolean;
  label: string;
  height?: number;
  className?: string;
}

const MARKER_SHAPE: Record<CandleMarker['kind'], SeriesMarker<Time>['shape']> = {
  entry: 'arrowUp',
  exit: 'arrowDown',
  stop: 'square',
  target: 'circle',
  elephant: 'arrowDown',
};
const MARKER_TITLE: Record<CandleMarker['kind'], string> = {
  entry: 'Entrada',
  exit: 'Saída',
  stop: 'Stop',
  target: 'Alvo',
  elephant: 'Elefante',
};

const seconds = (iso: string) => Math.floor(new Date(iso).getTime() / 1000) as UTCTimestamp;

const palette = () => {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim() || '#888888';
  return {
    bg: v('--surface'),
    text: v('--muted'),
    grid: v('--grid-line'),
    border: v('--border-strong'),
    gain: v('--gain'),
    loss: v('--loss'),
    warn: v('--warn'),
    info: v('--info'),
  };
};

type Palette = ReturnType<typeof palette>;

function withAlpha(hex: string, alpha: number) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex);
  if (!m?.[1]) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

const levelColor = (tone: ChartLevel['tone'], p: Palette) =>
  ({ stop: p.loss, target: p.gain, entry: p.info, exit: p.warn, blind: p.loss })[tone];

/** Ultima vela que abre ate `at` (marcadores caem na vela em que aconteceram). */
function snapTime(times: number[], at: number): number | null {
  let lo = 0;
  let hi = times.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if ((times[mid] ?? 0) <= at) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found < 0 ? null : (times[found] ?? null);
}

export function CandleChart({
  candles,
  markers = [],
  levels = [],
  showElephants = true,
  label,
  height = 380,
  className,
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const price = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null);
  const marks = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const lines = useRef<ReturnType<ISeriesApi<'Candlestick'>['createPriceLine']>[]>([]);

  const elephantTimes = useMemo(
    () => new Set(markers.filter((m) => m.kind === 'elephant').map((m) => seconds(m.t) as number)),
    [markers],
  );

  useEffect(() => {
    const el = host.current;
    if (!el) return;
    const apply = () => {
      const p = palette();
      chart.current?.applyOptions({
        layout: {
          background: { type: ColorType.Solid, color: p.bg },
          textColor: p.text,
          fontFamily: 'Inter Variable, system-ui, sans-serif',
        },
        grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
        rightPriceScale: { borderColor: p.border },
        timeScale: { borderColor: p.border },
      });
      price.current?.applyOptions({
        upColor: 'rgba(0,0,0,0)',
        borderUpColor: p.gain,
        wickUpColor: p.gain,
        downColor: p.loss,
        borderDownColor: p.loss,
        wickDownColor: p.loss,
      });
    };
    const c = createChart(el, {
      autoSize: true,
      layout: { attributionLogo: false },
      localization: { locale: 'pt-BR' },
      timeScale: { timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    });
    chart.current = c;
    price.current = c.addSeries(CandlestickSeries, { priceLineVisible: false });
    volume.current = c.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      priceLineVisible: false,
      lastValueVisible: false,
    });
    c.priceScale('volume').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    marks.current = createSeriesMarkers(price.current, []);
    apply();
    const observer = new MutationObserver(apply);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => {
      observer.disconnect();
      c.remove();
      chart.current = price.current = volume.current = marks.current = null;
      lines.current = [];
    };
  }, []);

  useEffect(() => {
    const series = price.current;
    const vol = volume.current;
    if (!series || !vol) return;
    const p = palette();
    const data: CandlestickData<Time>[] = candles.map((k) => {
      const time = seconds(k.t);
      const elephant = showElephants && elephantTimes.has(time);
      return {
        time,
        open: k.o,
        high: k.h,
        low: k.l,
        close: k.c,
        ...(elephant ? { borderColor: p.warn, wickColor: p.warn } : {}),
      };
    });
    series.setData(data);
    vol.setData(
      candles.map((k) => ({
        time: seconds(k.t),
        value: k.v,
        color: withAlpha(k.c >= k.o ? p.gain : p.loss, 0.45),
      })),
    );
    chart.current?.timeScale().fitContent();
  }, [candles, elephantTimes, showElephants]);

  useEffect(() => {
    const plugin = marks.current;
    if (!plugin) return;
    const p = palette();
    const times = candles.map((k) => seconds(k.t) as number);
    const out: SeriesMarker<Time>[] = [];
    for (const m of markers) {
      if (m.kind === 'elephant' && !showElephants) continue;
      const at = snapTime(times, seconds(m.t));
      if (at === null) continue;
      const elephant = m.kind === 'elephant';
      out.push({
        time: at as UTCTimestamp,
        position: m.kind === 'entry' || elephant ? 'belowBar' : 'aboveBar',
        shape: elephant ? 'arrowUp' : MARKER_SHAPE[m.kind],
        color:
          m.kind === 'entry'
            ? p.info
            : elephant
              ? p.warn
              : m.kind === 'target'
                ? p.gain
                : m.kind === 'stop'
                  ? p.loss
                  : p.text,
        text: elephant ? 'E' : MARKER_TITLE[m.kind],
        size: elephant ? 0.8 : 1,
      });
    }
    out.sort((a, b) => Number(a.time) - Number(b.time));
    plugin.setMarkers(out);
  }, [candles, markers, showElephants]);

  useEffect(() => {
    const series = price.current;
    if (!series) return;
    lines.current.forEach((l) => series.removePriceLine(l));
    const p = palette();
    lines.current = levels.map((lv) =>
      series.createPriceLine({
        price: lv.price,
        color: levelColor(lv.tone, p),
        lineWidth: 2,
        lineStyle: lv.tone === 'entry' ? LineStyle.Solid : LineStyle.Dashed,
        axisLabelVisible: true,
        title: lv.title,
      }),
    );
  }, [levels, candles]);

  return (
    <div>
      <div
        ref={host}
        role="img"
        aria-label={label}
        style={{ height }}
        className={cn('w-full overflow-hidden rounded-control border border-border', className)}
      />
      <p className="mt-1 text-right text-cap text-muted">
        Gráfico:{' '}
        <a
          href="https://www.tradingview.com/"
          target="_blank"
          rel="noopener noreferrer"
          className="underline underline-offset-2"
        >
          TradingView Lightweight Charts
        </a>
      </p>
    </div>
  );
}
