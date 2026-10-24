import { CircleCheck, CircleX, TriangleAlert } from 'lucide-react';
import { Toaster as Sonner, toast } from 'sonner';
import { useTheme } from '@/theme/ThemeProvider';

export function Toaster() {
  const { theme } = useTheme();
  return (
    <Sonner
      theme={theme}
      position="bottom-right"
      visibleToasts={3}
      closeButton
      offset={{ bottom: 16, right: 16 }}
      mobileOffset={{ bottom: 76, right: 16, left: 16 }}
      icons={{
        success: <CircleCheck className="size-5 text-gain" aria-hidden="true" />,
        warning: <TriangleAlert className="size-5 text-warn" aria-hidden="true" />,
        error: <CircleX className="size-5 text-loss" aria-hidden="true" />,
      }}
      toastOptions={{
        unstyled: true,
        classNames: {
          toast:
            'flex w-[min(420px,calc(100vw-32px))] items-start gap-3 rounded-xl border border-l-[5px] border-border-strong bg-surface px-3.5 py-3 text-text shadow-pop',
          success: 'border-l-gain',
          warning: 'border-l-warn',
          error: 'border-l-loss',
          icon: 'mt-0.5',
          content: 'flex flex-col',
          title: 'font-bold',
          description: 'text-sec text-muted',
          closeButton:
            'absolute -right-2 -top-2 grid size-7 cursor-pointer place-items-center rounded-full border border-border-strong bg-surface text-text',
        },
      }}
    />
  );
}

/** Sucesso some em 5 s; atencao e erro ficam ate fechar (design-system.md, 6). */
export const notify = {
  success: (title: string, description?: string) =>
    void toast.success(title, { description, duration: 5000 }),
  warn: (title: string, description?: string) =>
    void toast.warning(title, { description, duration: Infinity }),
  error: (title: string, description?: string) =>
    void toast.error(title, { description, duration: Infinity }),
};
