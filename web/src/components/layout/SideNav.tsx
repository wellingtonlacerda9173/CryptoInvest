import { Fragment } from 'react';
import { LogOut, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { Link, useMatch } from 'react-router-dom';
import { Tooltip } from '@/components/ui/tooltip';
import { useIsDesktop } from '@/lib/useMediaQuery';
import { cn } from '@/lib/utils';
import { NAV_ITEMS } from './nav';

// Link com string de classe (e nao funcao): o Tooltip (Slot) so mescla className em texto.
function ActiveLink({
  to,
  className,
  children,
}: {
  to: string;
  className: string;
  children: React.ReactNode;
}) {
  const active = useMatch({ path: to, end: false }) !== null;
  return (
    <Link
      to={to}
      aria-current={active ? 'page' : undefined}
      className={cn(
        className,
        active && 'bg-info-bg font-semibold text-text shadow-[inset_3px_0_0_var(--info)]',
      )}
    >
      {children}
    </Link>
  );
}

interface Props {
  collapsed: boolean;
  onToggle: () => void;
  alertCount: number;
  onLogout: () => void;
}

const itemBase =
  'relative flex min-h-11 items-center gap-3 rounded-control text-muted no-underline hover:bg-surface-2 hover:text-text';

export function SideNav({ collapsed, onToggle, alertCount, onLogout }: Props) {
  const desktop = useIsDesktop();
  const rail = collapsed || !desktop;

  const wrap = (label: string, node: React.ReactElement) =>
    rail ? (
      <Tooltip content={label} side="right">
        {node}
      </Tooltip>
    ) : (
      node
    );

  return (
    <aside
      data-rail={rail}
      className={cn(
        'hidden shrink-0 self-stretch border-r border-border bg-surface px-3 py-5 sm:block',
        rail ? 'w-[72px] px-2 py-4' : 'w-[248px]',
      )}
    >
      <div
        className={cn(
          'sticky top-5 flex min-h-[calc(100dvh-40px)] flex-col gap-1',
          rail && 'items-center',
        )}
      >
        <div
          className={cn(
            'flex items-center gap-2.5 pb-4 font-bold tracking-[-0.01em]',
            rail ? 'px-0' : 'px-2.5 pt-1',
          )}
        >
          <div
            aria-hidden="true"
            className="grid size-8 shrink-0 place-items-center rounded-control bg-primary font-extrabold text-primary-text"
          >
            C
          </div>
          {!rail && <span className="text-metric">CryptoInvest</span>}
        </div>

        <nav
          aria-label="Principal"
          className={cn('flex flex-col gap-0.5', rail ? 'items-center' : 'w-full')}
        >
          {NAV_ITEMS.map(({ to, label, icon: Icon }) => {
            const badge = to === '/controle' && alertCount > 0;
            return (
              <Fragment key={to}>
                {wrap(
                  label,
                  <ActiveLink
                    to={to}
                    className={cn(itemBase, rail ? 'w-12 justify-center' : 'px-3')}
                  >
                    <Icon className="size-5 shrink-0" aria-hidden="true" />
                    <span className={cn(rail && 'sr-only')}>{label}</span>
                    {badge ? (
                      rail ? (
                        <span className="absolute right-2 top-2 size-2.5 rounded-full bg-warn">
                          <span className="sr-only">{alertCount} alerta(s)</span>
                        </span>
                      ) : (
                        <span className="ml-auto rounded-pill border border-warn bg-warn-bg px-2 text-cap text-warn">
                          {alertCount}
                          <span className="sr-only"> alerta(s)</span>
                        </span>
                      )
                    ) : null}
                  </ActiveLink>,
                )}
              </Fragment>
            );
          })}
        </nav>

        <div className={cn('mt-auto flex flex-col gap-2', rail && 'items-center')}>
          {wrap(
            'Sair',
            <button
              type="button"
              onClick={onLogout}
              className={cn(
                itemBase,
                'cursor-pointer border-0 bg-transparent',
                rail ? 'w-12 justify-center' : 'w-full px-3 text-left',
              )}
              aria-label="Sair"
            >
              <LogOut className="size-5 shrink-0" aria-hidden="true" />
              {!rail && <span>Sair</span>}
            </button>,
          )}
          {desktop &&
            wrap(
              collapsed ? 'Expandir menu' : 'Recolher menu',
              <button
                type="button"
                onClick={onToggle}
                aria-label={collapsed ? 'Expandir menu' : 'Recolher menu'}
                className={cn(
                  itemBase,
                  'cursor-pointer border-0 bg-transparent',
                  rail ? 'w-12 justify-center' : 'w-full px-3 text-left',
                )}
              >
                {collapsed ? (
                  <PanelLeftOpen className="size-5 shrink-0" aria-hidden="true" />
                ) : (
                  <PanelLeftClose className="size-5 shrink-0" aria-hidden="true" />
                )}
                {!rail && <span>Recolher menu</span>}
              </button>,
            )}
        </div>
      </div>
    </aside>
  );
}
