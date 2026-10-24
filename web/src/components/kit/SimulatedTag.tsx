import type { ReactNode } from 'react';

export function SimulatedTag({ children = 'SIMULADO' }: { children?: ReactNode }) {
  return (
    <span className="inline-block rounded-[4px] border border-dashed border-info bg-info-bg px-1.5 py-px align-middle text-[11px] font-bold tracking-[0.05em] text-info">
      {children}
    </span>
  );
}
