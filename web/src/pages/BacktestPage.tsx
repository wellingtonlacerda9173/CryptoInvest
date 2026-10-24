import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '@/auth/AuthContext';
import { PageHeader } from '@/components/layout/PageHeader';
import { History } from './backtest/History';
import { JobView } from './backtest/JobView';
import { Wizard } from './backtest/Wizard';
import { DEFAULT_WIZARD, stateFromParams, type WizardState } from './backtest/wizardModel';

/** O id do backtest vai na URL (?teste=): F5 e link voltam para o mesmo progresso ou relatorio. */
export default function BacktestPage() {
  const { session } = useAuth();
  const [params, setParams] = useSearchParams();
  const [seed, setSeed] = useState<WizardState>(DEFAULT_WIZARD);
  const [wizardKey, setWizardKey] = useState(0);
  const jobId = params.get('teste');
  const canAct = session?.user.role === 'admin';

  const open = (id: string | null) => setParams(id ? { teste: id } : {});

  return (
    <>
      <PageHeader
        title="Backtest"
        description="Teste uma ideia no passado, sem risco, e leia o resultado com honestidade."
      />
      {jobId ? (
        <JobView
          key={jobId}
          jobId={jobId}
          canAct={canAct}
          onBack={() => open(null)}
          onRedo={(p) => {
            setSeed(stateFromParams(p));
            setWizardKey((k) => k + 1);
            open(null);
          }}
        />
      ) : (
        <>
          <Wizard key={wizardKey} initial={seed} canRun={canAct} onCreated={open} />
          <History onOpen={open} />
        </>
      )}
    </>
  );
}
