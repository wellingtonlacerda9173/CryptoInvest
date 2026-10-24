const MINUS = '−';

const formatters = new Map<number, Intl.NumberFormat>();
const nf = (digits: number) => {
  let f = formatters.get(digits);
  if (!f) {
    f = new Intl.NumberFormat('pt-BR', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
    formatters.set(digits, f);
  }
  return f;
};

const roundsToZero = (body: string) => Number(body.replace(/\./g, '').replace(',', '.')) === 0;

export const formatNumber = (value: number, digits = 2) => {
  const body = nf(digits).format(Math.abs(value));
  return value < 0 && !roundsToZero(body) ? `${MINUS}${body}` : body;
};

export const formatSigned = (value: number, digits = 2) => {
  const body = nf(digits).format(Math.abs(value));
  if (value < 0 && !roundsToZero(body)) return `${MINUS}${body}`;
  return value > 0 ? `+${body}` : body;
};

export const formatPercent = (value: number, digits = 2, signed = false) =>
  `${signed ? formatSigned(value, digits) : formatNumber(value, digits)}%`;

export type Direction = 'up' | 'down' | 'flat';
export const directionOf = (value: number): Direction =>
  value > 0 ? 'up' : value < 0 ? 'down' : 'flat';

export const formatClock = (date: Date | string | number) =>
  new Intl.DateTimeFormat('pt-BR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(date));

export const spokenSigned = (value: number, digits = 2) =>
  `${value < 0 ? 'menos ' : value > 0 ? 'mais ' : ''}${nf(digits).format(Math.abs(value))}`;
