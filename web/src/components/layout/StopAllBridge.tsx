import type { ReactNode } from 'react';
import { controlApi, useInvalidateAfterCommand } from '@/api/queries';
import { StopAllProvider, type StopAllFn } from '@/components/kit/StopAll';

/** Liga o "Parar tudo" (barra superior e tela de Controle) ao endpoint real e revalida o estado. */
export function StopAllBridge({ children }: { children: ReactNode }) {
  const refresh = useInvalidateAfterCommand();
  const stopAll: StopAllFn = async (request) => {
    try {
      return await controlApi.emergencyStop({
        liquidate: request.mode === 'liquidate',
        operator_name: request.operator || null,
        reason: request.reason || null,
      });
    } finally {
      void refresh();
    }
  };
  return <StopAllProvider stopAll={stopAll}>{children}</StopAllProvider>;
}
