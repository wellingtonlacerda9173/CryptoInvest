import { Rocket } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useOnboarding } from '@/api/queries';
import { buttonVariants } from '@/components/ui/button';

/** Lembrete para quem ainda nao terminou o primeiro acesso; some quando o onboarding esta completo. */
export function OnboardingNudge() {
  const { data } = useOnboarding();
  if (!data || data.completed) return null;
  return (
    <div
      role="note"
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-info bg-info-bg px-3.5 py-3"
    >
      <p className="flex items-start gap-2">
        <Rocket className="mt-0.5 size-5 shrink-0 text-info" aria-hidden="true" />
        <span>
          <strong>Falta pouco para o robô trabalhar.</strong>{' '}
          <span className="text-muted">{data.message}</span>
        </span>
      </p>
      <Link to="/onboarding" className={buttonVariants({ variant: 'primary' })}>
        Continuar a configuração
      </Link>
    </div>
  );
}
