import { useQueryClient } from '@tanstack/react-query';
import { CircleCheck, Eye, EyeOff, ShieldAlert, XCircle } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '@/api/http';
import { KEYS, onboardingApi, useCredentials } from '@/api/queries';
import { ReauthCancelled, useFreshAuth } from '@/auth/ReauthContext';
import { Chip } from '@/components/kit/Chip';
import { notify } from '@/components/kit/Toaster';
import { Button, buttonVariants } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';

function saveError(error: unknown): string {
  if (error instanceof ReauthCancelled) return 'Confirmação cancelada. A chave não foi salva.';
  if (error instanceof ApiError) {
    if (error.isNetwork) return 'Sem conexão com o servidor. A chave não foi salva.';
    if (error.status === 422 || error.status === 409 || error.status === 400)
      return (
        error.problem.detail ??
        'Esta chave não foi aceita. Confira se ela é só de leitura e negociação à vista.'
      );
  }
  return 'Não foi possível salvar a chave agora. Tente de novo.';
}

/** Write-only: depois de enviar, o segredo some do formulario e a tela so mostra o final da chave. */
export function BinanceStep({ onNext }: { onNext: () => void }) {
  const uid = useId();
  const qc = useQueryClient();
  const runFresh = useFreshAuth();
  const credentials = useCredentials();
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [showSecret, setShowSecret] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connected = credentials.data?.find((c) => c.usable) ?? null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setPending(true);
    setError(null);
    try {
      await runFresh(() =>
        onboardingApi.saveCredential({
          label: 'Principal',
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
        }),
      );
      notify.success('Chave conectada', 'Só leitura e negociação à vista, sem saque.');
      await qc.invalidateQueries({ queryKey: KEYS.credentials });
      setApiKey('');
    } catch (cause) {
      setError(saveError(cause));
    } finally {
      setApiSecret('');
      setPending(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-[20px] font-bold tracking-[-0.01em]">Conectar a Binance</h1>
      <p className="text-muted">
        Para testar não precisa de chaves. Você pode pular e conectar depois.
      </p>

      <div
        role="note"
        className="flex items-start gap-2.5 rounded-xl border border-warn bg-warn-bg px-3.5 py-3 text-sec"
      >
        <ShieldAlert className="mt-0.5 size-5 shrink-0 text-warn" aria-hidden="true" />
        <ul className="flex list-disc flex-col gap-1 pl-4">
          <li>
            Crie a chave <strong>só com leitura e negociação à vista</strong>. Uma chave que pode
            sacar dinheiro é recusada.
          </li>
          <li>
            Na Binance, restrinja a chave ao endereço (IP) do computador do robô. Assim, mesmo que
            vaze, ela não funciona em outro lugar.
          </li>
          <li>O segredo é guardado no servidor e nunca mais aparece nesta tela.</li>
        </ul>
      </div>

      {credentials.isPending ? (
        <div aria-busy="true" className="flex flex-col gap-3">
          <Skeleton className="h-11" />
          <Skeleton className="h-11" />
        </div>
      ) : connected ? (
        <div
          role="status"
          className="flex flex-wrap items-center gap-2.5 rounded-xl border border-gain bg-gain-bg px-3.5 py-3"
        >
          <CircleCheck className="size-5 text-gain" aria-hidden="true" />
          <strong>Chave …{connected.api_key_last4} conectada</strong>
          <Chip tone="ok" icon={false}>
            Só leitura e spot
          </Chip>
          <Chip tone="ok" icon={false}>
            Saque desligado
          </Chip>
          {connected.permissions.ip_restricted === false ? (
            <Chip tone="warn">Sem restrição de IP</Chip>
          ) : null}
        </div>
      ) : (
        <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-4" noValidate>
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
                type={showSecret ? 'text' : 'password'}
                autoComplete="new-password"
                spellCheck={false}
                value={apiSecret}
                onChange={(e) => setApiSecret(e.target.value)}
              />
              <Button
                aria-pressed={showSecret}
                aria-label={showSecret ? 'Ocultar segredo' : 'Mostrar segredo'}
                onClick={() => setShowSecret((v) => !v)}
              >
                {showSecret ? (
                  <EyeOff className="size-4" aria-hidden="true" />
                ) : (
                  <Eye className="size-4" aria-hidden="true" />
                )}
              </Button>
            </div>
          </Field>
          {error ? (
            <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
              <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              {error}
            </p>
          ) : null}
          <div className="flex flex-wrap gap-2.5">
            <Button
              type="submit"
              variant="primary"
              disabled={pending || !apiKey.trim() || !apiSecret.trim()}
            >
              {pending ? 'Testando a chave…' : 'Testar e salvar a chave'}
            </Button>
          </div>
        </form>
      )}

      <div className="flex flex-wrap items-center gap-2.5">
        {connected ? (
          <Button variant="primary" onClick={onNext}>
            Continuar
          </Button>
        ) : (
          <Button onClick={onNext}>Pular por agora</Button>
        )}
        <Link to="/dashboard" className={buttonVariants({ variant: 'ghost' })}>
          Salvar e continuar depois
        </Link>
      </div>
    </div>
  );
}
