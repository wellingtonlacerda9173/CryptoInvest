import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { CandleBar, CandleMarker } from '@/api/marketSchemas';
import { decisionSentence, TIER_TEXT, type DecisionGroup } from '@/lib/chartDecisions';
import type { LiveView } from '@/lib/liveCandle';
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
  /** Velas do banco + vela em formação + lacunas. Sem isso, desenha só `candles`. */
  live?: LiveView;
  /** Decisões já agrupadas por vela (faixa própria sob as velas). */
  decisions?: DecisionGroup[];
  /** Muda ao trocar de par/período: só aí o gráfico reenquadra (não a cada cotação nova). */
  fitKey?: string;
}

interface Hover {
  x: number;
  y: number;
  group: DecisionGroup;
}

const localDay = new Intl.DateTimeFormat('pt-BR', { day: '2-digit', month: '2-digit' });
const localClock = new Intl.DateTimeFormat('pt-BR', { hour: '2-digit', minute: '2-digit' });
const localFull = new Intl.DateTimeFormat('pt-BR', {
  day: '2-digit',
  month: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});
const asMs = (t: Time) => (typeof t === 'number' ? t * 1000 : Number.NaN);
const timeFormatter = (t: Time) => localFull.format(asMs(t));
const tickFormatter = (t: Time, type: TickMarkType) =>
  type === TickMarkType.Time || type === TickMarkType.TimeWithSeconds
    ? localClock.format(asMs(t))
    : localDay.format(asMs(t));

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

/** Velas visíveis ao abrir em tela estreita (o resto fica à esquerda, arrastando). */
const MOBILE_BARS = 36;
const RIGHT_PAD = 5;

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
  live,
  decisions = [],
  fitKey,
}: Props) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const price = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null);
  const marks = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const lane = useRef<ISeriesApi<'Line'> | null>(null);
  const laneMarks = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const nowLine = useRef<IPriceLine | null>(null);
  const fitted = useRef<string | null>(null);
  const groups = useRef<Map<number, DecisionGroup>>(new Map());
  const [hover, setHover] = useState<Hover | null>(null);
  const [pinned, setPinned] = useState<Hover | null>(null);
  const [width, setWidth] = useState(0);

  const shown = hover ?? pinned;

  const bars = useMemo(
    () =>
      live?.bars ??
      candles.map((k) => ({
        t: new Date(k.t).getTime(),
        o: k.o,
        h: k.h,
        l: k.l,
        c: k.c,
        forming: false,
      })),
    [live, candles],
  );
  const gaps = live?.gaps;
  const livePrice = live?.livePrice ?? null;

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
      localization: { locale: 'pt-BR', timeFormatter },
      timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: tickFormatter },
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
    lane.current = c.addSeries(LineSeries, {
      priceScaleId: 'lane',
      color: 'transparent',
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => ({ priceRange: { minValue: -1, maxValue: 1 } }),
    });
    c.priceScale('right').applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });
    c.priceScale('volume').applyOptions({ scaleMargins: { top: 0.76, bottom: 0.12 } });
    c.priceScale('lane').applyOptions({ scaleMargins: { top: 0.9, bottom: 0.01 }, visible: false });
    marks.current = createSeriesMarkers(price.current, []);
    laneMarks.current = createSeriesMarkers(lane.current, []);
    c.subscribeCrosshairMove((param) => {
      const at = typeof param.time === 'number' ? param.time : null;
      const group = at === null ? undefined : groups.current.get(at);
      setHover(group && param.point ? { x: param.point.x, y: param.point.y, group } : null);
    });
    c.subscribeClick((param) => {
      // Toque no celular: fixa a dica da decisão (não há "passar o mouse"). Toque fora fecha.
      const at = typeof param.time === 'number' ? param.time : null;
      const group = at === null ? undefined : groups.current.get(at);
      setPinned(group && param.point ? { x: param.point.x, y: param.point.y, group } : null);
    });
    setWidth(el.clientWidth);
    c.timeScale().subscribeSizeChange(() => setWidth(el.clientWidth));
    apply();
    const observer = new MutationObserver(apply);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => {
      observer.disconnect();
      c.remove();
      chart.current = price.current = volume.current = marks.current = null;
      lane.current = laneMarks.current = null;
      nowLine.current = null;
      fitted.current = null;
      lines.current = [];
    };
  }, []);

  useEffect(() => {
    const series = price.current;
    const vol = volume.current;
    const laneSeries = lane.current;
    if (!series || !vol || !laneSeries) return;
    const p = palette();
    const rows: (CandlestickData<Time> | WhitespaceData<Time>)[] = bars.map((k) => {
      const time = Math.floor(k.t / 1000) as UTCTimestamp;
      const elephant = showElephants && elephantTimes.has(time);
      const style = k.forming
        ? { color: withAlpha(p.info, 0.3), borderColor: p.info, wickColor: p.info }
        : elephant
          ? { borderColor: p.warn, wickColor: p.warn }
          : {};
      return { time, open: k.o, high: k.h, low: k.l, close: k.c, ...style };
    });
    for (const g of gaps ?? []) rows.push({ time: Math.floor(g / 1000) as UTCTimestamp });
    rows.sort((a, b) => Number(a.time) - Number(b.time));
    series.setData(rows);
    laneSeries.setData(rows.map((r) => ({ time: r.time, value: 0 })));
    vol.setData(
      candles.map((k) => ({
        time: seconds(k.t),
        value: k.v,
        color: withAlpha(k.c >= k.o ? p.gain : p.loss, 0.45),
      })),
    );
    if (fitKey === undefined || fitted.current !== fitKey) {
      const scale = chart.current?.timeScale();
      // Celular: 96 velas num gráfico estreito viram um borrão; mostra só as mais recentes (dá para arrastar).
      // A folga à direita deixa o rótulo "vela atual" inteiro dentro do gráfico.
      const narrow = host.current ? host.current.clientWidth < 520 : false;
      const first = narrow && rows.length > MOBILE_BARS ? rows.length - MOBILE_BARS : -1;
      scale?.setVisibleLogicalRange({ from: first, to: rows.length + RIGHT_PAD });
      fitted.current = fitKey ?? null;
    }
  }, [bars, gaps, candles, elephantTimes, showElephants, fitKey]);

  useEffect(() => {
    const plugin = marks.current;
    if (!plugin) return;
    const p = palette();
    const times = bars.map((k) => Math.floor(k.t / 1000));
    const out: SeriesMarker<Time>[] = [];
    const forming = [...bars].reverse().find((k) => k.forming);
    if (forming) {
      out.push({
        time: Math.floor(forming.t / 1000) as UTCTimestamp,
        position: 'belowBar',
        shape: 'arrowUp',
        color: p.info,
        text: 'vela atual',
        size: 0.6,
      });
    }
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
  }, [bars, markers, showElephants]);

  useEffect(() => {
    const plugin = laneMarks.current;
    if (!plugin) return;
    const p = palette();
    groups.current = new Map(decisions.map((g) => [g.time, g]));
    plugin.setMarkers(
      decisions.map((g) => ({
        time: g.time as UTCTimestamp,
        position: 'inBar',
        shape: g.tier === 'blocked' ? 'square' : 'circle',
        color: g.tier === 'entered' ? p.gain : g.tier === 'blocked' ? p.warn : p.text,
        size: g.tier === 'entered' ? 1.6 : g.tier === 'blocked' ? 1.1 : 0.22,
        text: g.items.length > 1 ? String(g.items.length) : '',
      })),
    );
  }, [decisions, bars]);

  useEffect(() => {
    const series = price.current;
    if (!series) return;
    if (nowLine.current) series.removePriceLine(nowLine.current);
    nowLine.current =
      livePrice === null
        ? null
        : series.createPriceLine({
            price: livePrice,
            color: palette().info,
            lineWidth: 1,
            lineStyle: LineStyle.Dotted,
            axisLabelVisible: true,
            title: 'Agora',
          });
  }, [livePrice]);

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
  }, [levels, bars]);

  return (
    <div>
      <div className="relative">
        <div
          ref={host}
          role="img"
          aria-label={label}
          style={{ height }}
          className={cn('w-full overflow-hidden rounded-control border border-border', className)}
        />
        {shown ? (
          <div
            role="presentation"
            data-testid="dica-decisao"
            style={{
              left: Math.min(Math.max(shown.x - 120, 4), Math.max(width - 288 - 4, 4)),
              top: Math.max(shown.y - 8, 4),
            }}
            className="pointer-events-none absolute z-10 max-w-[min(18rem,calc(100%-8px))] -translate-y-full rounded-control border border-border-strong bg-surface p-2 text-cap shadow-lg"
          >
            <p className="font-bold">
              {localFull.format(shown.group.time * 1000)} · {TIER_TEXT[shown.group.tier]}
            </p>
            <ul className="mt-1 flex flex-col gap-1">
              {shown.group.items.slice(0, 3).map((d) => (
                <li key={d.id}>{decisionSentence(d)}</li>
              ))}
              {shown.group.items.length > 3 ? (
                <li className="text-muted">e mais {shown.group.items.length - 3}</li>
              ) : null}
            </ul>
          </div>
        ) : null}
      </div>
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
