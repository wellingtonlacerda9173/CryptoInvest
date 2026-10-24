import { Lock } from 'lucide-react';
import { useId, useState, type ClipboardEvent, type DragEvent } from 'react';
import { ApiError } from '@/api/http';
import { controlApi, KEYS, useLiveGate } from '@/api/queries';
import { useQueryClient } from '@tanstack/react-query';
import { useFreshAuth } from '@/auth/ReauthContext';
import { CardError } from '@/components/kit/CardError';
import { Chip } from '@/components/kit/Chip';
import { IneligibleSeal } from '@/components/kit/IneligibleSeal';
import { LiveCriteria } from '@/components/kit/LiveCriteria';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/input';
import { Skeleton } from '@/components/ui/skeleton';

const PHRASE = 'OPERAR COM DINHEIRO REAL';
const blockPaste = (e: ClipboardEvent | DragEvent) => e.preventDefault();

/**
 * Nesta fase o Live nao e ativavel (a API responde 409): a tela mostra os criterios reais e o botao
 * fica indisponivel. A frase fixa e digitada sem colar; o campo so abre quando tudo estiver cumprido.
 */
export function LiveGateCard({
  isAdmin,
  allowOpenCalendar,
}: {
  isAdmin: boolean;
  allowOpenCalendar: boolean;
}) {
  const uid = useId();
  const qc = useQueryClient();
  const runFresh = useFreshAuth();
  const gate = useLiveGate();
  const [phrase, setPhrase] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const g = gate.data;
  const allMet = !!g && g.criteria.length > 0 && g.criteria.every((c) => c.status === 'met');
  const canType = isAdmin && !!g && g.live_available && g.approved && allMet;
  const phraseOk = phrase === PHRASE;

  const activate = async () => {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      await runFresh(() => controlApi.activateLive({ phrase, code: code || null }));
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.problem.detail
          ? cause.problem.detail
          : 'Live ainda não disponível.',
      );
      setPhrase('');
      setCode('');
    } finally {
      setPending(false);
    }
  };

  return (
    <Card aria-labelledby="live-gate-t">
      <CardHeader>
        <CardTitle id="live-gate-t">
          <Lock className="size-5" aria-hidden="true" /> Trava do Live (dinheiro real)
        </CardTitle>
        <Chip tone="bad" icon={Lock}>
          Bloqueado
        </Chip>
      </CardHeader>
      {g ? (
        <>
          {allowOpenCalendar || g.ineligible_reason ? (
            <IneligibleSeal reason={allowOpenCalendar ? null : g.ineligible_reason} />
          ) : null}
          <LiveCriteria criteria={g.criteria} />
          <Field
            label={
              <>
                Para liberar, digite exatamente: <strong>{PHRASE}</strong>
              </>
            }
            htmlFor={`${uid}-phrase`}
            help={
              canType
                ? 'Digite a frase sem copiar e colar. Você também precisará do código do app autenticador.'
                : 'Bloqueado até todos os critérios serem cumpridos. Você também precisará do código do app autenticador.'
            }
            helpId={`${uid}-phrase-h`}
          >
            <Input
              id={`${uid}-phrase`}
              value={phrase}
              disabled={!canType}
              autoComplete="off"
              spellCheck={false}
              aria-describedby={`${uid}-phrase-h`}
              placeholder={canType ? '' : 'Bloqueado'}
              onChange={(e) => setPhrase(e.target.value)}
              onPaste={blockPaste}
              onDrop={blockPaste}
            />
          </Field>
          {canType ? (
            <Field label="Código do app autenticador (2FA)" htmlFor={`${uid}-code`}>
              <Input
                id={`${uid}-code`}
                inputMode="numeric"
                maxLength={6}
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              />
            </Field>
          ) : null}
          {error ? (
            <p role="alert" className="text-sec text-loss">
              {error}
            </p>
          ) : null}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              disabled={!canType || !phraseOk || code.length !== 6 || pending}
              aria-busy={pending || undefined}
              onClick={() => void activate()}
            >
              <Lock className="size-4" aria-hidden="true" />
              {g.live_available ? 'Liberar Live' : 'Live ainda não disponível'}
            </Button>
            {!g.live_available &&
            g.unavailable_reason?.replace(/\.$/, '') !== 'Live ainda não disponível' ? (
              <span className="text-sec text-muted">{g.unavailable_reason}</span>
            ) : null}
          </div>
        </>
      ) : gate.isError ? (
        <CardError
          error={gate.error}
          onRetry={() => void qc.invalidateQueries({ queryKey: KEYS.liveGate })}
        />
      ) : (
        <div className="flex flex-col gap-3" aria-busy="true">
          {[0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-6" />
          ))}
        </div>
      )}
    </Card>
  );
}
