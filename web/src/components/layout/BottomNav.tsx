import { Ellipsis, LogOut } from 'lucide-react';
import { NavLink } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/dialog';
import { cn } from '@/lib/utils';
import { BOTTOM_ITEMS, CALENDAR_ALERT_TEXT, MORE_ITEMS, type CalendarAlert } from './nav';
import { ThemeToggle } from './ThemeToggle';

const cell =
  'flex min-h-[52px] flex-col items-center justify-center gap-0.5 rounded-control text-[11px] text-muted no-underline';

const dotTone = (alert: NonNullable<CalendarAlert>) =>
  alert === 'expired' ? 'bg-loss' : 'bg-warn';

export function BottomNav({
  alertCount,
  calendarAlert = null,
  onLogout,
}: {
  alertCount: number;
  calendarAlert?: CalendarAlert;
  onLogout: () => void;
}) {
  return (
    <nav
      aria-label="Principal (celular)"
      className="fixed inset-x-0 bottom-0 z-[55] grid grid-cols-5 border-t border-border-strong bg-surface px-1 pb-[max(4px,env(safe-area-inset-bottom))] pt-1 sm:hidden"
    >
      {BOTTOM_ITEMS.map(({ to, label, short, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            cn(cell, 'relative', isActive && 'bg-info-bg font-bold text-text')
          }
        >
          <Icon className="size-5" aria-hidden="true" />
          {short ?? label}
          {to === '/controle' && alertCount > 0 ? (
            <span className="absolute right-3 top-1.5 size-2.5 rounded-full bg-warn">
              <span className="sr-only">{alertCount} alerta(s)</span>
            </span>
          ) : null}
        </NavLink>
      ))}
      <Sheet>
        <SheetTrigger asChild>
          <button
            type="button"
            className={cn(cell, 'relative cursor-pointer border-0 bg-transparent')}
          >
            <Ellipsis className="size-5" aria-hidden="true" />
            Mais
            {calendarAlert ? (
              <span
                className={cn(
                  'absolute right-3 top-1.5 size-2.5 rounded-full',
                  dotTone(calendarAlert),
                )}
              >
                <span className="sr-only">{CALENDAR_ALERT_TEXT[calendarAlert]}</span>
              </span>
            ) : null}
          </button>
        </SheetTrigger>
        <SheetContent>
          <SheetTitle className="text-[18px] font-bold">Mais opções</SheetTitle>
          <SheetDescription className="sr-only">
            Outras telas e preferências do painel.
          </SheetDescription>
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {MORE_ITEMS.map(({ to, label, icon: Icon }) => (
              <li key={to}>
                <SheetClose asChild>
                  <NavLink
                    to={to}
                    className={({ isActive }) =>
                      cn(
                        'flex min-h-11 items-center gap-3 rounded-control px-3 font-medium text-text no-underline hover:bg-surface-2',
                        isActive && 'bg-info-bg font-semibold',
                      )
                    }
                  >
                    <Icon className="size-5" aria-hidden="true" />
                    {label}
                    {to === '/calendario' && calendarAlert ? (
                      <span className={cn('ml-auto size-2.5 rounded-full', dotTone(calendarAlert))}>
                        <span className="sr-only">{CALENDAR_ALERT_TEXT[calendarAlert]}</span>
                      </span>
                    ) : null}
                  </NavLink>
                </SheetClose>
              </li>
            ))}
          </ul>
          <ThemeToggle className="w-full" />
          <Button variant="ghost" onClick={onLogout}>
            <LogOut className="size-4" aria-hidden="true" />
            Sair
          </Button>
          <SheetClose asChild>
            <Button>Fechar</Button>
          </SheetClose>
        </SheetContent>
      </Sheet>
    </nav>
  );
}
