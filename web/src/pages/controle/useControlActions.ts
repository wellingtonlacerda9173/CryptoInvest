import { controlApi, useInvalidateAfterCommand, type OperatorBody } from '@/api/queries';
import type { Command } from '@/api/schemas';
import { useFreshAuth } from '@/auth/ReauthContext';
import { COMMAND_STATE_TEXT } from '@/components/kit/CommandList';
import { commandFailureMessage } from '@/lib/commandFailure';
import { notify } from '@/components/kit/Toaster';

export type ControlAction = 'pause' | 'resume' | 'reactivate' | 'power-on' | 'power-off';

function failure(error: unknown): Error {
  return new Error(commandFailureMessage(error), { cause: error });
}

const report = (command: Command | null | undefined, fallback: string) => {
  const s = command ? COMMAND_STATE_TEXT[command.state] : null;
  notify.success(fallback, s ? `${s.label}: ${s.help}` : undefined);
};

/** Acoes do robo: nome + motivo vao no registro; reativar pede TOTP recente (reautenticacao). */
export function useControlActions() {
  const refresh = useInvalidateAfterCommand();
  const runFresh = useFreshAuth();

  const body = ({ operator, reason }: { operator: string; reason: string }): OperatorBody => ({
    operator_name: operator || null,
    reason,
  });

  const guard = async (work: () => Promise<void>) => {
    try {
      await work();
    } catch (error) {
      throw failure(error);
    } finally {
      void refresh();
    }
  };

  return {
    pause: (input: { operator: string; reason: string }) =>
      guard(async () => {
        report(await controlApi.pause(body(input)), 'Pausa enviada ao robô');
      }),
    resume: (input: { operator: string; reason: string }) =>
      guard(async () => {
        report(await controlApi.resume(body(input)), 'Retomada enviada ao robô');
      }),
    reactivate: (input: { operator: string; reason: string }) =>
      guard(async () => {
        const out = await runFresh(() =>
          controlApi.reactivate({ operator_name: input.operator, reason: input.reason }),
        );
        report(out, 'Reativação enviada ao robô');
      }),
    power: (on: boolean, input: { operator: string; reason: string }) =>
      guard(async () => {
        const out = await controlApi.power({
          on,
          ...body(input),
          operator_name: input.operator || null,
        });
        notify.success(
          on ? 'Pedido para ligar enviado' : 'Pedido para desligar enviado',
          out.message,
        );
      }),
  };
}
