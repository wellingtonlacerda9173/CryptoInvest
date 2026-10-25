import {
  AlignLeft,
  ArrowLeftRight,
  CalendarClock,
  ChartCandlestick,
  ChartLine,
  Clock,
  LayoutDashboard,
  Settings,
  SlidersHorizontal,
  UserRound,
  type LucideIcon,
} from 'lucide-react';
import type { CalendarAlert } from '@/api/calendarQueries';

export type { CalendarAlert };

export const CALENDAR_ALERT_TEXT = {
  expiring: 'Calendário: atenção',
  expired: 'Calendário: vencido',
} as const;

export interface NavItem {
  to: string;
  label: string;
  short?: string;
  icon: LucideIcon;
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/dashboard', label: 'Dashboard', short: 'Início', icon: LayoutDashboard },
  { to: '/controle', label: 'Controle', icon: SlidersHorizontal },
  { to: '/mercado', label: 'Mercado', icon: ChartCandlestick },
  { to: '/calendario', label: 'Calendário', icon: CalendarClock },
  { to: '/decisoes', label: 'Decisões ao vivo', icon: AlignLeft },
  { to: '/operacoes', label: 'Operações', icon: ArrowLeftRight },
  { to: '/desempenho', label: 'Desempenho', icon: ChartLine },
  { to: '/backtest', label: 'Backtest', icon: Clock },
  { to: '/configuracoes', label: 'Configurações', icon: Settings },
  { to: '/conta', label: 'Minha conta', icon: UserRound },
];

const byTo = (to: string) => NAV_ITEMS.find((i) => i.to === to)!;

export const BOTTOM_ITEMS: NavItem[] = ['/dashboard', '/controle', '/operacoes', '/desempenho'].map(
  byTo,
);
export const MORE_ITEMS: NavItem[] = [
  '/mercado',
  '/calendario',
  '/decisoes',
  '/backtest',
  '/configuracoes',
  '/conta',
].map(byTo);
