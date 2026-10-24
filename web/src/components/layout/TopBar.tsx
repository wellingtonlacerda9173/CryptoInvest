import { useAccount, useBotStatus } from '@/api/queries';
import { useAuth } from '@/auth/AuthContext';
import { resolveTopBarFace, type StatusFace } from '@/lib/bot-state';
import { useConnection } from '@/realtime/ConnectionProvider';
import { TopBarView } from './TopBarView';

const LOADING_FACE: StatusFace = { tone: 'off', label: 'Conectando…', reason: 'aguardando o robô' };
const ERROR_FACE: StatusFace = {
  tone: 'offline',
  label: 'Sem dados do robô',
  reason: 'tentando de novo',
};

export function TopBar() {
  const { data, isError } = useBotStatus();
  // Saldo e resultado do dia vêm SÓ de /account (a mesma fonte do Dashboard): /status mede o dia pela
  // conta de risco do robô, que pode divergir. Nunca misturar as duas na tela.
  const { data: account } = useAccount();
  const { disconnected, runnerConnected } = useConnection();
  const { session } = useAuth();
  const face = resolveTopBarFace({
    disconnected,
    runnerConnected,
    status: data
      ? {
          state: data.state,
          stopsBlind: data.stops_blind,
          reason: data.halt_reason ?? data.pause_reasons[0]?.reason ?? null,
        }
      : null,
    loadFailed: isError,
    loading: LOADING_FACE,
    failed: ERROR_FACE,
  });

  return (
    <TopBarView
      face={face}
      mode={data?.mode ?? account?.mode ?? null}
      balance={account?.equity ?? null}
      dayPnl={account?.day_pnl ?? null}
      dayPnlPct={account?.day_pnl_pct ?? null}
      stale={disconnected || runnerConnected === false}
      operatorName={session?.user.name}
      canStop={session?.user.role === 'admin'}
    />
  );
}
