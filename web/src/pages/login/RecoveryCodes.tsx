import { Download, Printer, TriangleAlert } from 'lucide-react';
import { useId, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';

const FILE_NAME = 'codigos-de-recuperacao-cryptoinvest.txt';

const fileText = (codes: string[]) =>
  [
    'CryptoInvest - códigos de recuperação',
    'Cada código vale uma vez. Guarde em lugar seguro e não compartilhe.',
    '',
    ...codes,
    '',
  ].join('\n');

function download(codes: string[]) {
  const url = URL.createObjectURL(
    new Blob([fileText(codes)], { type: 'text/plain;charset=utf-8' }),
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = FILE_NAME;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function print(codes: string[]) {
  const frame = document.createElement('iframe');
  frame.setAttribute('aria-hidden', 'true');
  frame.style.cssText = 'position:fixed;width:0;height:0;border:0;';
  document.body.append(frame);
  const doc = frame.contentDocument;
  const win = frame.contentWindow;
  if (!doc || !win) return frame.remove();
  const items = codes.map((c) => `<li>${c.replace(/[<>&]/g, '')}</li>`).join('');
  doc.open();
  doc.write(
    `<!doctype html><html lang="pt-BR"><title>Códigos de recuperação</title><body style="font-family:sans-serif"><h1>CryptoInvest</h1><p>Códigos de recuperação. Cada código vale uma vez. Guarde em lugar seguro.</p><ol style="font:20px monospace;line-height:1.8">${items}</ol></body></html>`,
  );
  doc.close();
  win.addEventListener('afterprint', () => frame.remove());
  win.focus();
  win.print();
}

/** Mostrados UMA vez, so em memoria: ao sair desta tela nao ha como ve-los de novo. */
export function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  const uid = useId();
  const [saved, setSaved] = useState(false);
  return (
    <div className="flex flex-col gap-4">
      <h2 className="text-[18px] font-bold">Guarde seus códigos de recuperação</h2>
      <p className="flex items-start gap-2 rounded-xl border border-warn bg-warn-bg px-3 py-2.5 text-sec">
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warn" aria-hidden="true" />
        <span>
          Eles aparecem só agora. Se você perder o celular, cada código deixa entrar uma vez. Sem
          eles, só o administrador consegue refazer seu acesso.
        </span>
      </p>
      <ol
        aria-label="Códigos de recuperação"
        className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-xl border border-border-strong bg-surface-2 p-3 font-mono text-[15px] tabular-nums"
      >
        {codes.map((c) => (
          <li key={c} className="list-inside list-decimal">
            {c}
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2.5">
        <Button onClick={() => download(codes)}>
          <Download className="size-4" aria-hidden="true" /> Baixar
        </Button>
        <Button onClick={() => print(codes)}>
          <Printer className="size-4" aria-hidden="true" /> Imprimir
        </Button>
      </div>
      <label
        htmlFor={`${uid}-ok`}
        className="flex min-h-11 cursor-pointer items-center gap-2.5 font-semibold"
      >
        <Checkbox id={`${uid}-ok`} checked={saved} onCheckedChange={(v) => setSaved(v === true)} />
        Guardei os códigos em lugar seguro.
      </label>
      <Button variant="primary" disabled={!saved} onClick={onDone}>
        Continuar
      </Button>
    </div>
  );
}
