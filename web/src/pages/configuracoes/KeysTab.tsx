import { useQueryClient } from '@tanstack/react-query';
import { Eye, EyeOff, KeyRound, Lock, ShieldAlert, XCircle } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';
import { ApiError } from '@/api/http';
import { KEYS, useCredentials } from '@/api/queries';
import type { Credential } from '@/api/schemas';
import { credentialsApi } from '@/api/settingsQueries';
import { ReauthCancelled, useFreshAuth } from '@/auth/ReauthContext';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { ConfirmDialog } from '@/components/kit/ConfirmDialog';
import { EmptyState } from '@/components/kit/EmptyState';
import { notify } from '@/components/kit/Toaster';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';

const when = (iso: string | null) =>
  iso
    ? new Intl.DateTimeFormat('pt-BR', {
        day: '2-digit',
        month: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      }).format(new Date(iso))
    : 'nunca';

/** Mensagem da API quando houver (ex.: "remova a chave só com o robô desligado"); nunca ecoa segredos. */
function failure(error: unknown, what: string): string {
  if (error instanceof ReauthCancelled) return `Confirmação cancelada. ${what}`;
  if (error instanceof ApiError) {
    if (error.isNetwork) return `Sem conexão com o servidor. ${what}`;
    if (error.problem.detail) return error.problem.detail;
  }
  return `Não foi possível concluir agora. ${what}`;
}

const NOT_SAVED = 'A chave não foi salva.';

function SecretFields({
  apiKey,
  apiSecret,
  setApiKey,
  setApiSecret,
}: {
  apiKey: string;
  apiSecret: string;
  setApiKey: (v: string) => void;
  setApiSecret: (v: string) => void;
}) {
  const uid = useId();
  const [show, setShow] = useState(false);
  return (
    <>
      <Field label="Chave da API (API Key)" htmlFor={`${uid}-k`}>
        <Input
          id={`${uid}-k`}
          autoComplete="off"
          spellCheck={false}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
      </Field>
      <Field label="Segredo (Secret)" htmlFor={`${uid}-s`}>
        <div className="flex gap-2">
          <Input
            id={`${uid}-s`}
            type={show ? 'text' : 'password'}
            autoComplete="new-password"
            spellCheck={false}
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
          />
          <Button
            aria-pressed={show}
            aria-label={show ? 'Ocultar segredo' : 'Mostrar segredo'}
            onClick={() => setShow((v) => !v)}
          >
            {show ? (
              <EyeOff className="size-4" aria-hidden="true" />
            ) : (
              <Eye className="size-4" aria-hidden="true" />
            )}
          </Button>
        </div>
      </Field>
    </>
  );
}

function PermissionChips({ c }: { c: Credential }) {
  const p = c.permissions;
  return (
    <span className="flex flex-wrap gap-1.5">
      <Chip tone={c.usable ? 'ok' : 'bad'}>
        {c.usable ? 'Pronta para uso' : 'Não pode ser usada'}
      </Chip>
      <Chip tone={p.can_read ? 'ok' : 'bad'} icon={false}>
        {p.can_read ? 'Leitura' : 'Sem leitura'}
        {p.can_trade ? ' e spot' : ''}
      </Chip>
      <Chip tone={p.can_withdraw ? 'bad' : 'ok'}>
        {p.can_withdraw ? 'Saque ligado: recusada' : 'Saque desligado'}
      </Chip>
      {p.ip_restricted === false ? (
        <Chip tone="warn">Sem restrição de IP</Chip>
      ) : p.ip_restricted ? (
        <Chip tone="ok" icon={false}>
          IP restrito
        </Chip>
      ) : (
        <Chip tone="off" icon={false}>
          IP não verificado
        </Chip>
      )}
    </span>
  );
}

function KeyRow({ c }: { c: Credential }) {
  const qc = useQueryClient();
  const runFresh = useFreshAuth();
  const [rotate, setRotate] = useState(false);
  const [remove, setRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const refresh = () => qc.invalidateQueries({ queryKey: KEYS.credentials });

  const verify = async () => {
    setBusy(true);
    try {
      await credentialsApi.verify(c.id);
      await refresh();
      notify.success('Chave testada', 'As permissões foram conferidas de novo.');
    } catch (cause) {
      notify.error('Não foi possível testar', failure(cause, 'Tente de novo.'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex flex-col gap-2.5 border-b border-border py-3.5 first:pt-0 last:border-0 last:pb-0">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="flex flex-col">
          <strong>
            {c.label} <span className="font-normal text-muted">…{c.api_key_last4}</span>
          </strong>
          <span className="text-sec text-muted">
            Conferida em {when(c.last_verified_at)} · usada em {when(c.last_used_at)}
          </span>
        </span>
        <span className="flex flex-wrap gap-2">
          <Button disabled={busy} onClick={() => void verify()}>
            Testar
          </Button>
          <Button onClick={() => setRotate(true)}>Trocar chave</Button>
          <Button onClick={() => setRemove(true)}>Remover</Button>
        </span>
      </div>
      <PermissionChips c={c} />
      {c.warnings.length > 0 ? (
        <ul className="list-disc pl-5 text-sec text-muted">
          {c.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}

      <ConfirmDialog
        open={rotate}
        onOpenChange={(open) => {
          setRotate(open);
          if (!open) {
            setApiKey('');
            setApiSecret('');
          }
        }}
        title={`Trocar a chave ${c.label}`}
        description="Cole a chave nova. O segredo antigo é apagado e a nova chave é testada antes de valer."
        confirmLabel="Testar e trocar"
        confirmDisabled={!apiKey.trim() || !apiSecret.trim()}
        onConfirm={async () => {
          try {
            await runFresh(() =>
              credentialsApi.rotate(c.id, { api_key: apiKey.trim(), api_secret: apiSecret.trim() }),
            );
          } catch (cause) {
            throw new Error(failure(cause, 'A chave não foi trocada.'), { cause });
          } finally {
            setApiSecret('');
          }
          await refresh();
          notify.success('Chave trocada', 'Só leitura e negociação à vista, sem saque.');
        }}
      >
        <SecretFields
          apiKey={apiKey}
          apiSecret={apiSecret}
          setApiKey={setApiKey}
          setApiSecret={setApiSecret}
        />
      </ConfirmDialog>

      <ConfirmDialog
        open={remove}
        onOpenChange={setRemove}
        tone="danger"
        title={`Remover a chave ${c.label}?`}
        description="O robô não consegue operar sem uma chave. A remoção só é aceita com o robô desligado."
        confirmLabel="Remover a chave"
        onConfirm={async () => {
          try {
            await runFresh(() => credentialsApi.remove(c.id));
          } catch (cause) {
            throw new Error(failure(cause, 'A chave não foi removida.'), { cause });
          }
          await refresh();
          notify.success('Chave removida');
        }}
      />
    </li>
  );
}

function AddKey() {
  const uid = useId();
  const qc = useQueryClient();
  const runFresh = useFreshAuth();
  const [label, setLabel] = useState('Principal');
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setPending(true);
    setError(null);
    try {
      await runFresh(() =>
        credentialsApi.create({
          label: label.trim(),
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
        }),
      );
      await qc.invalidateQueries({ queryKey: KEYS.credentials });
      notify.success('Chave conectada', 'Só leitura e negociação à vista, sem saque.');
      setApiKey('');
    } catch (cause) {
      setError(failure(cause, NOT_SAVED));
    } finally {
      setApiSecret('');
      setPending(false);
    }
  };

  return (
    <form
      onSubmit={(e) => void submit(e)}
      className="flex max-w-[460px] flex-col gap-4 border-t border-border pt-4"
      noValidate
      aria-label="Adicionar chave da Binance"
    >
      <h3 className="text-[16px] font-bold">Adicionar uma chave</h3>
      <Field label="Nome da chave" htmlFor={`${uid}-l`}>
        <Input
          id={`${uid}-l`}
          value={label}
          maxLength={40}
          onChange={(e) => setLabel(e.target.value)}
        />
      </Field>
      <SecretFields
        apiKey={apiKey}
        apiSecret={apiSecret}
        setApiKey={setApiKey}
        setApiSecret={setApiSecret}
      />
      {error ? (
        <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
          <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {error}
        </p>
      ) : null}
      <div>
        <Button
          type="submit"
          variant="primary"
          disabled={pending || !label.trim() || !apiKey.trim() || !apiSecret.trim()}
        >
          {pending ? 'Testando a chave…' : 'Testar e salvar a chave'}
        </Button>
      </div>
    </form>
  );
}

export function KeysTab({ canEdit }: { canEdit: boolean }) {
  const credentials = useCredentials(canEdit);
  if (!canEdit)
    return (
      <p
        role="note"
        className="flex items-start gap-2 rounded-xl border border-border-strong bg-surface-2 px-3.5 py-2.5 text-sec"
      >
        <Lock className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        As chaves da corretora só aparecem para administradores. Você tem acesso de leitor.
      </p>
    );
  return (
    <div className="flex flex-col gap-4">
      <div
        role="note"
        className="flex items-start gap-2.5 rounded-xl border border-warn bg-warn-bg px-3.5 py-3 text-sec"
      >
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-warn" aria-hidden="true" />
        <ul className="flex list-disc flex-col gap-1 pl-4">
          <li>
            Crie a chave <strong>só com leitura e negociação à vista</strong>. Uma chave com
            permissão de saque é recusada.
          </li>
          <li>
            Na Binance, restrinja a chave ao endereço (IP) do computador do robô: se vazar, ela não
            funciona em outro lugar.
          </li>
          <li>
            O segredo é guardado no servidor e nunca mais aparece. Só os 4 últimos caracteres da
            chave.
          </li>
          <li>Para remover uma chave, desligue o robô antes.</li>
        </ul>
      </div>
      {credentials.isError ? (
        <CardError
          error={credentials.error}
          onRetry={() => void credentials.refetch()}
          retrying={credentials.isFetching}
        />
      ) : credentials.isPending ? (
        <Skeleton className="h-20" aria-busy="true" />
      ) : credentials.data.length === 0 ? (
        <EmptyState icon={KeyRound} title="Nenhuma chave conectada">
          Para testar (Paper) não precisa de chave. Adicione quando for operar de verdade.
        </EmptyState>
      ) : (
        <ul className="flex flex-col" aria-label="Chaves da Binance">
          {credentials.data.map((c) => (
            <KeyRow key={c.id} c={c} />
          ))}
        </ul>
      )}
      <AddKey />
    </div>
  );
}
