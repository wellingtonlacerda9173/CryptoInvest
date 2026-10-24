import { Info } from 'lucide-react';
import type { ReactNode } from 'react';
import { ThemeToggle } from './ThemeToggle';

export function PageHeader({ title, description }: { title: string; description?: ReactNode }) {
  return (
    <>
      <div className="flex flex-wrap items-end justify-between gap-4 max-sm:items-start">
        <div>
          <h1 className="text-page font-bold tracking-[-0.02em]">{title}</h1>
          {description ? <p className="text-muted">{description}</p> : null}
        </div>
        <ThemeToggle />
      </div>
      {import.meta.env.VITE_MOCK === '1' ? <ExampleBanner /> : null}
    </>
  );
}

export function ExampleBanner() {
  return (
    <div
      role="note"
      className="flex items-center gap-2.5 rounded-xl border border-dashed border-border-strong bg-surface px-3.5 py-2 text-sec text-muted max-sm:text-cap"
    >
      <Info className="size-4 shrink-0" aria-hidden="true" />
      <span>
        <strong className="text-text">EXEMPLO</strong> - todos os números desta tela são fictícios,
        só para avaliar o desenho. Nada aqui é real nem foi calculado pelo robô.
      </span>
    </div>
  );
}
