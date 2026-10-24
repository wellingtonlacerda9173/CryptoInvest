import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { StopAllRequest } from '@/api/schemas';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ApiError } from '@/api/http';
import type { Command } from '@/api/schemas';
import { notify } from './Toaster';
import { ConfirmDialog } from './ConfirmDialog';
import { StopAllButton, StopAllProvider } from './StopAll';

const setup = (
  opts: {
    mode?: 'paper' | 'live';
    fail?: boolean | ApiError;
    canStop?: boolean;
    command?: Partial<Command>;
  } = {},
) => {
  const stopAll = vi.fn<(r: StopAllRequest) => Promise<Command | void>>(() =>
    opts.fail
      ? Promise.reject(opts.fail === true ? new Error('x') : opts.fail)
      : Promise.resolve(opts.command as Command | undefined),
  );
  const user = userEvent.setup();
  render(
    <TooltipProvider>
      <StopAllProvider stopAll={stopAll}>
        <StopAllButton mode={opts.mode ?? 'paper'} operatorName="ana" canStop={opts.canStop} />
      </StopAllProvider>
    </TooltipProvider>,
  );
  return { stopAll, user };
};

describe('Parar tudo', () => {
  it('abrir o dialogo nao dispara nada: exige confirmacao', async () => {
    const { stopAll, user } = setup();
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    expect(
      await screen.findByRole('alertdialog', { name: 'Parar tudo agora?' }),
    ).toBeInTheDocument();
    expect(stopAll).not.toHaveBeenCalled();
  });

  it('Voltar e Esc cancelam sem chamar stopAll', async () => {
    const { stopAll, user } = setup();
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    await user.click(await screen.findByRole('button', { name: 'Voltar (Esc)' }));
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    await screen.findByRole('alertdialog');
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    expect(stopAll).not.toHaveBeenCalled();
  });

  it('opcao segura e a padrao e confirmar so bloqueia novas operacoes', async () => {
    const { stopAll, user } = setup();
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    expect(await screen.findByRole('radio', { name: /Só bloquear novas operações/ })).toBeChecked();
    await user.click(screen.getByRole('button', { name: 'Confirmar: parar tudo' }));
    await waitFor(() => expect(stopAll).toHaveBeenCalledTimes(1));
    expect(stopAll).toHaveBeenCalledWith({ mode: 'block_new', operator: 'ana', reason: '' });
  });

  it('vender tudo exige nome e motivo', async () => {
    const { stopAll, user } = setup();
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    await user.click(await screen.findByRole('radio', { name: /vender tudo/ }));
    const confirm = screen.getByRole('button', { name: 'Confirmar: parar tudo' });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText('Motivo'), 'Notícia inesperada');
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() =>
      expect(stopAll).toHaveBeenCalledWith({
        mode: 'liquidate',
        operator: 'ana',
        reason: 'Notícia inesperada',
      }),
    );
  });

  it('no LIVE, vender exige ainda o aceite de dinheiro real', async () => {
    const { user } = setup({ mode: 'live' });
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    await user.click(await screen.findByRole('radio', { name: /vender tudo/ }));
    await user.type(screen.getByLabelText('Motivo'), 'Emergência');
    const confirm = screen.getByRole('button', { name: 'Confirmar: parar tudo' });
    expect(confirm).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /dinheiro real/ }));
    expect(confirm).toBeEnabled();
  });

  it('o modo aparece em destaque no dialogo', async () => {
    const { user } = setup({ mode: 'live' });
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toHaveTextContent('LIVE');
    expect(dialog).toHaveTextContent('Isto vende em dinheiro real.');
  });

  it('perda de resposta/5xx mantem o dialogo e diz "nao confirmado", nunca "nada mudou"', async () => {
    const { user } = setup({ fail: new ApiError(503, { title: 'Indisponível' }) });
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    await user.click(await screen.findByRole('button', { name: 'Confirmar: parar tudo' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Não confirmado');
    expect(alert).toHaveTextContent('Histórico de controle');
    expect(alert).not.toHaveTextContent('Nada mudou');
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  });

  it('4xx claro afirma que nada mudou', async () => {
    const { user } = setup({ fail: new ApiError(422, { title: 'Inválido' }) });
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    await user.click(await screen.findByRole('button', { name: 'Confirmar: parar tudo' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Nada mudou');
  });

  it('o aviso de sucesso segue o estado do comando, sem prometer venda sem ack', async () => {
    const success = vi.spyOn(notify, 'success').mockImplementation(() => undefined as never);
    const { user } = setup({ command: { state: 'pending_delivery', liquidate: true } });
    await user.click(screen.getByRole('button', { name: 'Parar tudo' }));
    await user.click(await screen.findByRole('radio', { name: /vender tudo/ }));
    await user.type(screen.getByLabelText('Motivo'), 'x');
    await user.click(screen.getByRole('button', { name: 'Confirmar: parar tudo' }));
    await waitFor(() => expect(success).toHaveBeenCalled());
    const [title, detail] = success.mock.calls[0]!;
    expect(title).toBe('Parada: aguardando entrega');
    expect(detail).toMatch(/ainda não recebeu/);
    expect(`${title} ${detail}`).not.toMatch(/sendo vendidas/);
    success.mockRestore();
  });

  it('leitor ve o botao, mas ele nao abre o dialogo', async () => {
    const { user } = setup({ canStop: false });
    const button = screen.getByRole('button', { name: 'Parar tudo' });
    expect(button).toHaveAttribute('aria-disabled', 'true');
    await user.click(button);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });
});

describe('ConfirmDialog generico', () => {
  it('so executa onConfirm ao clicar em confirmar', async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="Reativar?"
        confirmLabel="Confirmar"
        onConfirm={onConfirm}
      />,
    );
    expect(onConfirm).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Confirmar' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('confirmDisabled bloqueia o envio (inclusive por Enter)', async () => {
    const onConfirm = vi.fn();
    const user = userEvent.setup();
    render(
      <ConfirmDialog
        open
        onOpenChange={() => {}}
        title="x"
        confirmLabel="Ok"
        confirmDisabled
        onConfirm={onConfirm}
      >
        <input aria-label="campo" />
      </ConfirmDialog>,
    );
    await user.type(screen.getByLabelText('campo'), '{Enter}');
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
