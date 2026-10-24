import { Chip, type ChipTone } from '@/components/kit/Chip';
import { Card, CardTitle } from '@/components/ui/card';
import { ListTree } from 'lucide-react';

interface Row {
  tone: ChipTone;
  label: string;
  code: string;
  meaning: string;
  todo: string;
}

const ROWS: Row[] = [
  {
    tone: 'ok',
    label: 'Operando',
    code: 'RUNNING',
    meaning: 'Procurando oportunidades e gerenciando posições.',
    todo: 'Nada. Acompanhe.',
  },
  {
    tone: 'warn',
    label: 'Pausado',
    code: 'PAUSED',
    meaning:
      'Não abre novas operações. As posições abertas continuam protegidas pelo stop. Motivos possíveis: perda diária, divergência na conferência, você pausou.',
    todo: 'Leia o motivo. Retome em Controle (informa seu nome e o porquê).',
  },
  {
    tone: 'bad',
    label: 'Desligado por drawdown',
    code: 'HALTED',
    meaning: 'A queda chegou a 5%. O robô não opera mais sozinho, de propósito.',
    todo: 'Revise o que aconteceu e use Reativar (nome, motivo e código do app).',
  },
  {
    tone: 'bad',
    label: 'Falha ao gravar',
    code: 'FAILED_PERSISTENCE',
    meaning:
      'O banco de dados saiu do ar. Nada é perdido: os eventos ficam guardados no disco. Novas entradas ficam bloqueadas e os stops seguem ativos.',
    todo: 'Normalmente volta sozinho. Se passar de 5 min, chame o suporte.',
  },
  {
    tone: 'off',
    label: 'Parado',
    code: 'STOPPED',
    meaning: 'Encerrado de forma ordenada. Sem novas entradas.',
    todo: 'Ligue de novo quando quiser.',
  },
  {
    tone: 'off',
    label: 'Robô desligado',
    code: 'OFFLINE',
    meaning: 'O painel não recebe sinal do robô: ele está desligado ou sem conexão.',
    todo: 'Ligue o robô pelo Controle. Os números mostrados são os últimos conhecidos.',
  },
];

export function StatesTable() {
  return (
    <Card aria-labelledby="estados-t" className="lg:col-span-2">
      <CardTitle id="estados-t">
        <ListTree className="size-5" aria-hidden="true" /> Os estados do robô, em palavras simples
      </CardTitle>
      <div
        tabIndex={0}
        role="region"
        aria-label="Tabela dos estados do robô"
        className="hidden overflow-x-auto sm:block"
      >
        <table className="w-full border-collapse text-left">
          <thead>
            <tr className="text-sec text-muted">
              {['Estado', 'O que significa', 'O que fazer'].map((h) => (
                <th key={h} scope="col" className="border-b border-border px-2 py-2 font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((r) => (
              <tr key={r.code} className="align-top">
                <th scope="row" className="border-b border-border px-2 py-3 text-left font-normal">
                  <Chip tone={r.tone}>{r.label}</Chip>
                  <div className="mt-1 text-cap text-muted">{r.code}</div>
                </th>
                <td className="border-b border-border px-2 py-3">{r.meaning}</td>
                <td className="border-b border-border px-2 py-3">{r.todo}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="flex flex-col gap-3 sm:hidden" aria-label="Estados do robô">
        {ROWS.map((r) => (
          <li key={r.code} className="flex flex-col gap-1.5 rounded-xl border border-border p-3">
            <span className="flex items-center gap-2">
              <Chip tone={r.tone}>{r.label}</Chip>
              <span className="text-cap text-muted">{r.code}</span>
            </span>
            <p>{r.meaning}</p>
            <p className="text-sec text-muted">
              <strong className="text-text">O que fazer:</strong> {r.todo}
            </p>
          </li>
        ))}
      </ul>
    </Card>
  );
}
