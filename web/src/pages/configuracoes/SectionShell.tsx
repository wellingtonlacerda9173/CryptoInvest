import { Lock, RotateCcw, XCircle } from 'lucide-react';
import { useId, useState, type ReactNode } from 'react';
import { ConfirmDialog } from '@/components/kit/ConfirmDialog';
import { RiskIncreaseWarning } from '@/components/kit/RiskIncreaseWarning';
import { notify } from '@/components/kit/Toaster';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Field, Input } from '@/components/ui/input';
import type { SectionEditor } from './useSectionEditor';

/** Moldura comum das abas editaveis: permissao, aviso de aumento de risco, motivo e salvar. */
export function SectionShell({ editor, children }: { editor: SectionEditor; children: ReactNode }) {
  const uid = useId();
  const [understood, setUnderstood] = useState(false);
  const { validation } = editor;
  const increases = validation?.changes.filter((c) => c.increases_risk) ?? [];
  const errorCount = Object.keys(editor.fieldErrors).length;
  const reasonShort = editor.reason.trim().length < 3;
  const canSave = editor.canEdit && editor.valid && !reasonShort && !editor.saving;

  return (
    <div className="flex flex-col gap-4">
      {editor.canEdit ? null : (
        <p
          role="note"
          className="flex items-start gap-2 rounded-xl border border-border-strong bg-surface-2 px-3.5 py-2.5 text-sec"
        >
          <Lock className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          Você tem acesso de leitor: pode ver estes valores, mas só administradores alteram as
          configurações.
        </p>
      )}

      {children}

      {errorCount > 0 ? (
        <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
          <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {errorCount === 1
            ? 'Há 1 campo para corrigir antes de salvar.'
            : `Há ${errorCount} campos para corrigir antes de salvar.`}
        </p>
      ) : null}

      {increases.map((c) => (
        <RiskIncreaseWarning
          key={c.path}
          label={c.label}
          before={c.before_text}
          after={c.after_text}
          costText={c.cost_text || undefined}
        />
      ))}
      {validation?.valid && validation.activation_text ? (
        <p role="status" className="text-sec text-muted">
          {validation.activation_text}
        </p>
      ) : null}

      <div className="flex flex-col gap-3 border-t border-border pt-4">
        <p className="text-sec text-muted">
          Alterações valem a partir da próxima vela. Posições abertas mantêm o stop atual: o robô
          nunca afasta um stop.
        </p>
        <div className="max-w-[460px]">
          <Field
            label="Motivo da mudança"
            htmlFor={`${uid}-reason`}
            help="Fica no histórico, com o seu nome."
            helpId={`${uid}-reason-h`}
          >
            <Input
              id={`${uid}-reason`}
              value={editor.reason}
              maxLength={280}
              disabled={!editor.canEdit}
              aria-describedby={`${uid}-reason-h`}
              onChange={(e) => editor.setReason(e.target.value)}
            />
          </Field>
        </div>
        {editor.error ? (
          <p role="alert" className="flex items-start gap-1.5 text-sec text-loss">
            <XCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            {editor.error}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-2.5">
          <Button variant="primary" disabled={!canSave} onClick={() => void editor.save()}>
            {editor.saving
              ? 'Salvando…'
              : !editor.dirty
                ? 'Nada para salvar'
                : editor.checking
                  ? 'Conferindo…'
                  : 'Salvar alterações'}
          </Button>
          {editor.dirty ? <Button onClick={editor.discard}>Descartar</Button> : null}
          <Button variant="ghost" disabled={!editor.canEdit} onClick={editor.applyDefaults}>
            <RotateCcw className="size-4" aria-hidden="true" /> Voltar ao padrão seguro
          </Button>
        </div>
      </div>

      <ConfirmDialog
        open={editor.confirmOpen}
        onOpenChange={(open) => {
          editor.setConfirmOpen(open);
          if (!open) setUnderstood(false);
        }}
        title="Confirmar aumento de risco"
        description="Esta mudança aumenta o risco e só vale no próximo início do robô, sem posição aberta. Você vai confirmar sua identidade em seguida."
        confirmLabel="Confirmar e salvar"
        tone="danger"
        confirmDisabled={!understood}
        onConfirm={async () => {
          await editor.performSave(true);
          notify.success(
            'Configuração salva',
            'O aumento de risco vale no próximo início do robô.',
          );
        }}
      >
        <div className="flex flex-col gap-2.5">
          {increases.map((c) => (
            <RiskIncreaseWarning
              key={c.path}
              label={c.label}
              before={c.before_text}
              after={c.after_text}
              costText={c.cost_text || undefined}
            />
          ))}
          <label className="flex cursor-pointer items-start gap-2.5">
            <Checkbox
              checked={understood}
              onCheckedChange={(v) => setUnderstood(v === true)}
              className="mt-0.5"
            />
            <span>Entendo que isto aumenta o risco e quero salvar mesmo assim.</span>
          </label>
        </div>
      </ConfirmDialog>
    </div>
  );
}
