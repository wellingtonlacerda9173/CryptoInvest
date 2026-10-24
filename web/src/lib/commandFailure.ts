import { ApiError } from '@/api/http';
import { ReauthCancelled } from '@/auth/ReauthContext';

export const UNCONFIRMED =
  'Não confirmado — confira o Histórico de controle. O pedido pode ter chegado ao robô.';

/** 5xx, timeout e perda de resposta nao provam que nada chegou ao robo; so 4xx claro prova. */
export function isUnconfirmed(error: unknown): boolean {
  if (error instanceof ReauthCancelled) return false;
  if (error instanceof ApiError)
    return error.status === 0 || error.status === 408 || error.status >= 500;
  return true;
}

/** Mensagem de falha de um comando de controle, sem afirmar "nada mudou" quando nao se sabe. */
export function commandFailureMessage(error: unknown): string {
  if (error instanceof ReauthCancelled) return 'Confirmação cancelada. Nada mudou.';
  if (isUnconfirmed(error)) return UNCONFIRMED;
  if (error instanceof ApiError) {
    if (error.status === 409 && error.problem.detail) return error.problem.detail;
    if (error.status === 403) return 'Somente administradores podem fazer isto.';
  }
  return 'O robô não recebeu o pedido. Nada mudou. Tente de novo.';
}
