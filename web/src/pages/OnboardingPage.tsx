import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link, Navigate, useNavigate } from 'react-router-dom';
import { KEYS, useOnboarding } from '@/api/queries';
import { useAuth } from '@/auth/AuthContext';
import { CardError } from '@/components/kit/CardError';
import { ModeBadge } from '@/components/kit/ModeBadge';
import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { buttonVariants } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { cn } from '@/lib/utils';
import { BinanceStep } from './onboarding/BinanceStep';
import { RiskStep } from './onboarding/RiskStep';
import { StartStep } from './onboarding/StartStep';

const STEPS = ['Conectar a Binance', 'Configurar o risco', 'Iniciar no Paper'] as const;

export default function OnboardingPage() {
  const { session } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const onboarding = useOnboarding();
  const [step, setStep] = useState<1 | 2 | 3>(1);

  if (session?.user.role !== 'admin') return <Navigate to="/dashboard" replace />;

  const data = onboarding.data;
  const finish = () => {
    void qc.invalidateQueries({ queryKey: KEYS.onboarding });
    void qc.invalidateQueries({ queryKey: KEYS.status });
    navigate('/dashboard', { replace: true });
  };

  return (
    <main className="grid min-h-dvh place-items-center px-4 py-8">
      <div className="absolute right-4 top-4">
        <ThemeToggle />
      </div>
      <Card className="w-full max-w-[640px] gap-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-bold">
            <span className="sr-only">Etapa atual: </span>Passo {step} de 3
          </p>
          <span className="flex items-center gap-2 text-sec text-muted">
            Você começa em <ModeBadge mode="paper" />
          </span>
        </div>
        <ol aria-label="Passos" className="flex gap-1.5">
          {STEPS.map((label, i) => (
            <li
              key={label}
              aria-current={i + 1 === step ? 'step' : undefined}
              className={cn(
                'h-1.5 flex-1 rounded-pill',
                i + 1 <= step ? 'bg-info' : 'bg-surface-2 border border-border',
              )}
            >
              <span className="sr-only">{label}</span>
            </li>
          ))}
        </ol>

        {onboarding.isError ? (
          <CardError
            error={onboarding.error}
            onRetry={() => void onboarding.refetch()}
            retrying={onboarding.isFetching}
          />
        ) : !data ? (
          <div aria-busy="true" className="flex flex-col gap-3">
            <Skeleton className="h-7 w-2/3" />
            <Skeleton className="h-11" />
            <Skeleton className="h-11" />
          </div>
        ) : data.completed && step === 1 ? (
          <div className="flex flex-col gap-3">
            <h1 className="text-[20px] font-bold tracking-[-0.01em]">
              Seu robô já está configurado
            </h1>
            <p className="text-muted">
              Chaves, risco e teste no Paper estão em dia. Você pode ajustar tudo depois em
              Configurações.
            </p>
            <Link to="/dashboard" className={buttonVariants({ variant: 'primary' })}>
              Ir para o Dashboard
            </Link>
          </div>
        ) : step === 1 ? (
          <BinanceStep onNext={() => setStep(2)} />
        ) : step === 2 ? (
          <RiskStep onBack={() => setStep(1)} onNext={() => setStep(3)} />
        ) : (
          <StartStep onBack={() => setStep(2)} onDone={finish} />
        )}
      </Card>
    </main>
  );
}
