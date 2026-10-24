import { Moon, Sun } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useTheme } from '@/theme/ThemeProvider';

export function ThemeToggle({ className }: { className?: string }) {
  const { theme, toggleTheme } = useTheme();
  const Icon = theme === 'dark' ? Moon : Sun;
  return (
    <Button className={className} onClick={toggleTheme} data-testid="theme-toggle">
      <Icon className="size-4" aria-hidden="true" />
      <span>Tema escuro / claro</span>
    </Button>
  );
}
