import {
  AlignLeft,
  ArrowLeftRight,
  ChartCandlestick,
  ChartLine,
  Clock,
  LayoutDashboard,
  Settings,
  SlidersHorizontal,
  UserRound,
  type LucideIcon,
} from 'lucide-react';

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
  '/decisoes',
  '/backtest',
  '/configuracoes',
  '/conta',
].map(byTo);
