import { useQueryClient } from '@tanstack/react-query';
import { KeyRound, Monitor, ShieldCheck, XCircle } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';
import { ApiError } from '@/api/http';
import { accountApi, KEYS, useSessions } from '@/api/queries';
import { ROLE_LABEL } from '@/api/schemas';
import { useAuth } from '@/auth/AuthContext';
import { ReauthCancelled, useFreshAuth } from '@/auth/ReauthContext';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { EmptyState } from '@/components/kit/EmptyState';
import { notify } from '@/components/kit/Toaster';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';
import { RecoveryCodes } from './login/RecoveryCodes';

const MIN_PASSWORD = 12;

const when = (iso: string) =>
  new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso));

const device = (ua: string | null) => {
  if (!ua) return 'Dispositivo desconhecido';
  const browser = /Edg\//.test(ua)
    ? 'Edge'
    : /Chrome\//.test(ua)
      ? 'Chrome'
      : /Firefox\//.test(ua)
        ? 'Firefox'
        : /Safari\//.test(ua)
          ? 'Safari'
          : 'Navegador';
  const system = /Windows/.test(ua)
    ? 'Windows'
    : /Android/.test(ua)
      ? 'Android'
      : /iPhone|iPad/.test(ua)
        ? 'iOS'
        : /Mac OS/.test(ua)
          ? 'macOS'
          : /Linux/.test(ua)
            ? 'Linux'
            : '';
  return system ? `${browser} no ${system}` : browser;
};

function passwordFailure(error: unknown): string {
  if (error instanceof ReauthCancelled) return 'Confirmação cancelada. A senha não foi trocada.';
  if (error instanceof ApiError) {
    if (error.isNetwork) return 'Sem conexão com o servidor. A senha não foi trocada.';
    if (error.status === 401 || error.status === 403)
      return 'A senha atual não confere. Confira e tente de novo.';
    const first = error.problem.errors?.[0]?.message ?? error.problem.detail;
    if (first) return first;
  }
  return 'Não foi possível trocar a senha agora. Tente de novo.';
}

function PasswordCard() {
  const uid = useId();
  const runFresh = useFreshAuth();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch =
    again !== '' && again !== next ? 'As duas senhas novas precisam ser iguais.' : null;
  const short =
    next !== '' && next.length < MIN_PASSWORD ? `Use pelo menos ${MIN_PASSWORD} caracteres.` : null;
  const invalid = !current || !next || !again || !!mismatch || !!short;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (invalid) return;
    setPending(true);
    setError(null);
    try {
      await runFresh(() =>
        accountApi.changePassword({ current_password: current, new_password: next }),
      );
      notify.success('Senha trocada', 'Use a nova senha no próximo acesso.');
      setCurrent('');
      setNext('');
      setAgain('');
    } catch (cause) {
      setError(passwordFailure(cause));
    } finally {
      setPending(false);
    }
  };

  return (
    <Card aria-labelledby="senha-t">
      <CardHeader>
        <CardTitle id="senha-t">
          <KeyRound className="size-5" aria-hidden="true" /> Trocar a senha
        </CardTitle>
      </CardHeader>
      <form
        onSubmit={(e) => void submit(e)}
        className="flex max-w-[420px] flex-col gap-4"
        noValidate
      >
        <Field label="Senha atual" htmlFor={`${uid}-cur`}>
          <Input
            id={`${uid}-cur`}
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </Field>
        <Field
          label="Senha nova"
          htmlFor={`${uid}-new`}
          help={`Pelo menos ${MIN_PASSWORD} caracteres. Evite senhas comuns e o seu nome de usuário.`}
          helpId={`${uid}-new-h`}
          error={short}
          errorId={`${uid}-new-e`}
        >
          <Input
            id={`${uid}-new`}
            type="password"
            autoComplete="new-password"
            value={next}
            aria-invalid={short ? true : undefined}
            aria-describedby={short ? `${uid}-new-e` : `${uid}-new-h`}
            onChange={(e) => setNext(e.target.value)}
          />
        </Field>
        <Field
          label="Repita a senha nova"
          htmlFor={`${uid}-again`}
          error={mismatch}
          errorId={`${uid}-again-e`}
        >
          <Input
            id={`${uid}-again`}
            type="password"
            autoComplete="new-password"
            value={again}
            aria-invalid={mismatch ? true : undefined}
            aria-describedby={mismatch ? `${uid}-again-e` : undefined}
            onChange={(e) => setAgain(e.target.value)}
          />
        </Field>
        {error ? (
          <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
            <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            {error}
          </p>
        ) : null}
        <div>
          <Button type="submit" variant="primary" disabled={pending || invalid}>
            {pending ? 'Trocando…' : 'Trocar a senha'}
          </Button>
        </div>
      </form>
    </Card>
  );
}

function SessionsCard() {
  const qc = useQueryClient();
  const sessions = useSessions();
  const [busy, setBusy] = useState<string | null>(null);

  const revoke = async (id: string | 'others') => {
    setBusy(id);
    try {
      if (id === 'others') await accountApi.revokeOtherSessions();
      else await accountApi.revokeSession(id);
      notify.success(id === 'others' ? 'Outras sessões encerradas' : 'Sessão encerrada');
      await qc.invalidateQueries({ queryKey: KEYS.sessions });
    } catch {
      notify.error('Não foi possível encerrar', 'Nada mudou. Tente de novo.');
    } finally {
      setBusy(null);
    }
  };

  const others = sessions.data?.filter((s) => !s.current) ?? [];

  return (
    <Card aria-labelledby="sessoes-t">
      <CardHeader>
        <CardTitle id="sessoes-t">
          <Monitor className="size-5" aria-hidden="true" /> Onde você está conectado
        </CardTitle>
        {others.length > 0 ? (
          <Button disabled={busy !== null} onClick={() => void revoke('others')}>
            Encerrar as outras sessões
          </Button>
        ) : null}
      </CardHeader>
      {sessions.data ? (
        sessions.data.length > 0 ? (
          <ul className="flex flex-col">
            {sessions.data.map((s) => (
              <li
                key={s.id}
                className="flex flex-wrap items-center justify-between gap-3 border-b border-border py-3 first:pt-0 last:border-0 last:pb-0"
              >
                <div className="flex flex-col">
                  <span className="flex flex-wrap items-center gap-2 font-semibold">
                    {device(s.user_agent)}
                    {s.current ? (
                      <Chip tone="info" icon={false}>
                        Esta sessão
                      </Chip>
                    ) : null}
                  </span>
                  <span className="text-sec text-muted">
                    {s.ip ? `${s.ip} · ` : ''}último uso {when(s.last_seen_at)}
                  </span>
                </div>
                {s.current ? null : (
                  <Button
                    aria-label={`Encerrar a sessão de ${device(s.user_agent)}, último uso ${when(s.last_seen_at)}`}
                    disabled={busy !== null}
                    onClick={() => void revoke(s.id)}
                  >
                    Encerrar
                  </Button>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState icon={Monitor} title="Nenhuma sessão ativa" />
        )
      ) : sessions.isError ? (
        <CardError error={sessions.error} onRetry={() => void sessions.refetch()} />
      ) : (
        <Skeleton className="h-16" aria-busy="true" />
      )}
    </Card>
  );
}

function RecoveryCard() {
  const runFresh = useFreshAuth();
  const [codes, setCodes] = useState<string[] | null>(null);
  const [pending, setPending] = useState(false);

  const generate = async () => {
    setPending(true);
    try {
      const out = await runFresh(() => accountApi.newRecoveryCodes());
      setCodes(out.recovery_codes);
    } catch (cause) {
      if (!(cause instanceof ReauthCancelled))
        notify.error('Não foi possível gerar os códigos', 'Nada mudou. Tente de novo.');
    } finally {
      setPending(false);
    }
  };

  return (
    <Card aria-labelledby="rec-t">
      <CardHeader>
        <CardTitle id="rec-t">
          <ShieldCheck className="size-5" aria-hidden="true" /> Códigos de recuperação
        </CardTitle>
      </CardHeader>
      {codes ? (
        <RecoveryCodes codes={codes} onDone={() => setCodes(null)} />
      ) : (
        <>
          <p className="text-muted">
            Se perder o celular, cada código deixa você entrar uma vez. Gerar novos códigos invalida
            os antigos. Eles aparecem uma única vez.
          </p>
          <div>
            <Button disabled={pending} onClick={() => void generate()}>
              Gerar novos códigos
            </Button>
          </div>
        </>
      )}
    </Card>
  );
}

export default function ContaPage() {
  const { session } = useAuth();
  return (
    <>
      <PageHeader
        title="Minha conta"
        description={
          session
            ? `${session.user.name} · ${ROLE_LABEL[session.user.role]}. Senha, sessões e códigos de recuperação.`
            : 'Senha, sessões e códigos de recuperação.'
        }
      />
      <PasswordCard />
      <SessionsCard />
      <RecoveryCard />
    </>
  );
}
