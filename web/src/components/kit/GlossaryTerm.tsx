import { Popover } from 'radix-ui';
import { useState, type ReactNode } from 'react';
import { GLOSSARY, type GlossaryKey } from '@/lib/glossary';

/** Termo tecnico com explicacao: abre com mouse, foco (Tab) e toque. Texto vem so do glossario. */
export function GlossaryTerm({ term, children }: { term: GlossaryKey; children?: ReactNode }) {
  const [open, setOpen] = useState(false);
  const entry = GLOSSARY[term];

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Anchor asChild>
        <button
          type="button"
          aria-expanded={open}
          aria-haspopup="dialog"
          className="cursor-help border-0 border-b border-dotted border-muted bg-transparent p-0 text-inherit [font:inherit]"
          onPointerEnter={(e) => e.pointerType === 'mouse' && setOpen(true)}
          onPointerLeave={(e) => e.pointerType === 'mouse' && setOpen(false)}
          onFocus={() => setOpen(true)}
          onClick={() => setOpen(true)}
        >
          {children ?? entry.term}
        </button>
      </Popover.Anchor>
      <Popover.Portal>
        <Popover.Content
          role="tooltip"
          side="top"
          sideOffset={8}
          collisionPadding={12}
          onOpenAutoFocus={(e) => e.preventDefault()}
          onCloseAutoFocus={(e) => e.preventDefault()}
          onInteractOutside={(e) => {
            if ((e.target as HTMLElement | null)?.closest('[aria-haspopup="dialog"]'))
              e.preventDefault();
          }}
          className="anim-pop z-[85] max-w-[min(340px,calc(100vw-24px))] rounded-control border border-border-strong bg-surface-2 px-3 py-2.5 text-sec text-text shadow-pop"
        >
          <b className="mb-0.5 block">{entry.term}</b>
          {entry.text}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
