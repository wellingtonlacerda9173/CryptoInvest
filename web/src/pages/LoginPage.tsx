import { Eye, EyeOff, XCircle } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { http, ApiError } from '@/api/http';
import { onboardingSchema } from '@/api/schemas';
import { useAuth } from '@/auth/AuthContext';
import type { EnrollmentResult, EnrollmentStart, Session } from '@/auth/types';
import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/input';
import { RecoveryCodes } from './login/RecoveryCodes';

type Step = 'credentials' | 'code' | 'recovery' | 'activation' | 'qr' | 'codes';

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.isNetwork)
      return error.problem.detail ?? 'Não conseguimos falar com o servidor. Verifique a internet.';
    if (error.status === 429) {
      const wait = error.problem.retry_after_seconds;
      return wait
        ? `Muitas tentativas. Tente de novo em ${wait >= 90 ? `${Math.ceil(wait / 60)} min` : `${wait} s`}.`
        : 'Muitas tentativas. Tente de novo em alguns minutos.';
    }
    if (error.status === 401 || error.status === 403)
      return 'Usuário, senha ou código não conferem. Confira e tente de novo.';
  }
  return 'Não foi possível entrar agora. Tente de novo em instantes.';
}

const digitsOnly = (raw: string) => raw.replace(/\D/g, '').slice(0, 6);
const SESSION_ENDED = 'ci-session-ended';

function readSessionEnded(): boolean {
  try {
    const ended = sessionStorage.getItem(SESSION_ENDED) === '1';
    sessionStorage.removeItem(SESSION_ENDED);
    return ended;
  } catch {
    return false;
  }
}

export default function LoginPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const uid = useId();
  const [step, setStep] = useState<Step>('credentials');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [code, setCode] = useState('');
  const [recoveryCode, setRecoveryCode] = useState('');
  const [activation, setActivation] = useState('');
  const [enroll, setEnroll] = useState<EnrollmentStart | null>(null);
  const [enrolled, setEnrolled] = useState<EnrollmentResult | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice] = useState(() =>
    readSessionEnded() ? 'Sua sessão terminou por segurança. Entre de novo.' : null,
  );

  const explicitFrom = (location.state as { from?: string } | null)?.from;
  const from = explicitFrom ?? '/dashboard';
  // Na etapa dos codigos a sessao ja existe no servidor, mas so e adotada depois de guardar os codigos.
  if (auth.status === 'authenticated' && step !== 'codes') return <Navigate to={from} replace />;

  const run = async (action: () => Promise<void>) => {
    setPending(true);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError(messageFor(cause));
    } finally {
      setPending(false);
    }
  };

  const enter = async (session: Session) => {
    let target = from;
    if (!explicitFrom && session.user.role === 'admin') {
      const onboarding = await http('/onboarding', { schema: onboardingSchema }).catch(() => null);
      if (onboarding && !onboarding.completed) target = '/onboarding';
    }
    navigate(target, { replace: true });
  };

  const submitCredentials = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      const { stage } = await auth.login(username.trim(), password);
      setPassword('');
      setStep(stage === 'enrollment' ? 'activation' : 'code');
    });
  };

  const submitCode = (value: string) =>
    void run(async () => enter(await auth.verifySecondFactor({ code: value })));

  const submitRecovery = (e: FormEvent) => {
    e.preventDefault();
    void run(async () =>
      enter(await auth.verifySecondFactor({ recoveryCode: recoveryCode.trim() })),
    );
  };

  const submitActivation = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      setEnroll(await auth.startEnrollment(activation.trim()));
      setActivation('');
      setStep('qr');
    });
  };

  const submitEnrollCode = (value: string) =>
    void run(async () => {
      setEnrolled(await auth.confirmEnrollment(value));
      setCode('');
      setEnroll(null);
      setStep('codes');
    });

  const onCode = (raw: string, submit: (v: string) => void) => {
    const digits = digitsOnly(raw);
    setCode(digits);
    if (digits.length === 6 && !pending) submit(digits);
  };

  const backToStart = () => {
    setStep('credentials');
    setCode('');
    setRecoveryCode('');
    setEnroll(null);
    setEnrolled(null);
    setError(null);
  };

  const codeField = (submit: (v: string) => void, help: string) => (
    <>
      <Field
        label="Código do aplicativo autenticador"
        htmlFor={`${uid}-c`}
        help={help}
        helpId={`${uid}-c-h`}
      >
        <Input
          id={`${uid}-c`}
          inputMode="numeric"
          autoComplete="one-time-code"
          autoFocus
          maxLength={9}
          value={code}
          onChange={(e) => onCode(e.target.value, submit)}
          aria-describedby={`${uid}-c-h`}
          className="text-center text-[20px] font-semibold tracking-[0.5em] tabular-nums"
        />
      </Field>
      <ErrorLine message={error} />
    </>
  );

  return (
    <main className="grid min-h-dvh place-items-center px-4 py-8">
      <div className="absolute right-4 top-4">
        <ThemeToggle />
      </div>
      <Card className="w-full max-w-[400px] gap-4">
        <div className="flex flex-col items-center gap-2 text-center">
          <div
            aria-hidden="true"
            className="grid size-10 place-items-center rounded-control bg-primary text-[20px] font-extrabold text-primary-text"
          >
            C
          </div>
          <p className="text-metric font-bold">CryptoInvest</p>
          <h1 className="text-[20px] font-bold tracking-[-0.01em]">
            {step === 'activation' || step === 'qr' || step === 'codes'
              ? 'Proteja o seu acesso'
              : 'Entre para ver o seu robô'}
          </h1>
        </div>

        {notice && step === 'credentials' ? (
          <p
            role="status"
            className="rounded-xl border border-warn bg-warn-bg px-3 py-2.5 text-sec"
          >
            {notice}
          </p>
        ) : null}

        {step === 'credentials' ? (
          <form onSubmit={submitCredentials} className="flex flex-col gap-4" noValidate>
            <Field label="Usuário" htmlFor={`${uid}-u`}>
              <Input
                id={`${uid}-u`}
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </Field>
            <Field label="Senha" htmlFor={`${uid}-p`}>
              <div className="flex gap-2">
                <Input
                  id={`${uid}-p`}
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
                <Button
                  aria-pressed={showPassword}
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? 'Ocultar senha' : 'Mostrar senha'}
                >
                  {showPassword ? (
                    <EyeOff className="size-4" aria-hidden="true" />
                  ) : (
                    <Eye className="size-4" aria-hidden="true" />
                  )}
                  <span className="max-sm:sr-only">{showPassword ? 'Ocultar' : 'Mostrar'}</span>
                </Button>
              </div>
            </Field>
            <ErrorLine message={error} />
            <Button type="submit" variant="primary" disabled={pending || !username || !password}>
              {pending ? 'Entrando…' : 'Continuar'}
            </Button>
          </form>
        ) : null}

        {step === 'code' ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (code.length === 6) submitCode(code);
            }}
            className="flex flex-col gap-4"
            noValidate
          >
            {codeField(submitCode, 'Digite os 6 números do app.')}
            <Button type="submit" variant="primary" disabled={pending || code.length !== 6}>
              {pending ? 'Entrando…' : 'Entrar'}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setStep('recovery');
                setError(null);
              }}
            >
              Usar código de recuperação
            </Button>
            <Button variant="ghost" onClick={backToStart}>
              Voltar
            </Button>
          </form>
        ) : null}

        {step === 'recovery' ? (
          <form onSubmit={submitRecovery} className="flex flex-col gap-4" noValidate>
            <Field
              label="Código de recuperação"
              htmlFor={`${uid}-r`}
              help="Use um dos códigos que você guardou ao configurar o aplicativo. Cada um vale uma vez."
              helpId={`${uid}-r-h`}
            >
              <Input
                id={`${uid}-r`}
                autoComplete="off"
                autoFocus
                spellCheck={false}
                value={recoveryCode}
                onChange={(e) => setRecoveryCode(e.target.value)}
                aria-describedby={`${uid}-r-h`}
              />
            </Field>
            <ErrorLine message={error} />
            <Button
              type="submit"
              variant="primary"
              disabled={pending || recoveryCode.trim() === ''}
            >
              {pending ? 'Entrando…' : 'Entrar'}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                setStep('code');
                setError(null);
              }}
            >
              Usar o código do aplicativo
            </Button>
          </form>
        ) : null}

        {step === 'activation' ? (
          <form onSubmit={submitActivation} className="flex flex-col gap-4" noValidate>
            <p className="text-muted">
              Antes de entrar, configure o aplicativo autenticador no seu celular. Para começar,
              digite o código de ativação que o administrador entregou a você.
            </p>
            <Field label="Código de ativação" htmlFor={`${uid}-a`}>
              <Input
                id={`${uid}-a`}
                autoComplete="off"
                autoFocus
                spellCheck={false}
                value={activation}
                onChange={(e) => setActivation(e.target.value)}
              />
            </Field>
            <ErrorLine message={error} />
            <Button type="submit" variant="primary" disabled={pending || activation.trim() === ''}>
              {pending ? 'Verificando…' : 'Continuar'}
            </Button>
            <Button variant="ghost" onClick={backToStart}>
              Voltar
            </Button>
          </form>
        ) : null}

        {step === 'qr' && enroll ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (code.length === 6) submitEnrollCode(code);
            }}
            className="flex flex-col gap-4"
            noValidate
          >
            <ol className="list-inside list-decimal text-muted">
              <li>Abra o aplicativo autenticador no celular.</li>
              <li>Escaneie o código abaixo.</li>
              <li>Digite os 6 números que o app mostrar.</li>
            </ol>
            <img
              src={enroll.qrDataUri}
              alt="Código QR para o aplicativo autenticador"
              width={200}
              height={200}
              className="mx-auto size-[200px] rounded-xl bg-white p-2"
            />
            <details className="text-sec">
              <summary className="cursor-pointer font-semibold">Não consegue escanear?</summary>
              <p className="mt-1.5 text-muted">Digite esta chave no aplicativo:</p>
              <code className="mt-1 block break-all rounded-control bg-surface-2 px-3 py-2 tabular-nums">
                {enroll.secret}
              </code>
            </details>
            {codeField(submitEnrollCode, 'Digite os 6 números do app para confirmar.')}
            <Button type="submit" variant="primary" disabled={pending || code.length !== 6}>
              {pending ? 'Confirmando…' : 'Confirmar'}
            </Button>
          </form>
        ) : null}

        {step === 'codes' && enrolled ? (
          <RecoveryCodes
            codes={enrolled.recoveryCodes}
            onDone={() => {
              const session = enrolled.session;
              setEnrolled(null);
              auth.finishEnrollment(session);
              void enter(session);
            }}
          />
        ) : null}

        {step !== 'codes' ? (
          <p className="text-center text-sec text-muted">
            Não encontra sua conta? Fale com o administrador.
          </p>
        ) : null}
      </Card>
    </main>
  );
}

function ErrorLine({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
      <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      {message}
    </p>
  );
}
