import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import { afterEach, describe, expect, it } from 'vitest';
import { makeCommand, makeStatus } from '@/mocks/mockState';
import { FRESH_REQUIRED, problem, renderPage, stubApi } from '@/test/render';
import ControlePage from './ControlePage';

const accepted = (patch = {}) => ({ ...makeCommand(patch), message: 'ok' });
const MUTATING = ['POST', 'PUT', 'PATCH', 'DELETE'];

async function fillOperatorDialog(dialogName: RegExp, reason = 'revisei o ocorrido') {
  const dialog = await screen.findByRole('alertdialog', { name: dialogName });
  await userEvent.type(within(dialog).getByLabelText('Motivo'), reason);
  return dialog;
}

describe('Controle: dado o painel real', () => {
  afterEach(() => {
    toast.dismiss();
  });

  it('Dado_Leitor_Quando_TentaAgir_Entao_NenhumaMutacaoEhEnviada', async () => {
    const api = stubApi({ 'GET /status': () => makeStatus({ state: 'HALTED', halted: true }) });
    renderPage(<ControlePage />, { role: 'reader' });
    expect(await screen.findByRole('switch')).toBeDisabled();
    expect(screen.getByRole('button', { name: /Pausar novas entradas/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Reativar/ })).toBeDisabled();
    // Parar tudo e so aparencia de botao: clicar e Enter nao abrem dialogo nem enviam nada
    const stop = screen.getByRole('button', { name: /Parar tudo agora/ });
    await userEvent.click(stop);
    stop.focus();
    await userEvent.keyboard('{Enter}');
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(api.calls.filter((c) => MUTATING.includes(c.method))).toEqual([]);
  });

  it('Dado_RoboHalted_Quando_AdminReativa_Entao_PedeTotpEEnviaOperadorEMotivo', async () => {
    let posts = 0;
    const api = stubApi({
      'GET /status': () => makeStatus({ state: 'HALTED', halted: true, halt_reason: 'drawdown' }),
      'POST /control/reactivate': () => {
        posts += 1;
        return posts === 1 ? FRESH_REQUIRED() : accepted({ name: 'reactivate' });
      },
    });
    renderPage(<ControlePage />);
    expect(await screen.findByRole('switch')).toBeDisabled();
    await userEvent.click(await screen.findByRole('button', { name: /Reativar/ }));
    const dialog = await fillOperatorDialog(/revisou o que aconteceu/);
    expect(within(dialog).getByRole('button', { name: 'Reativar o robô' })).toBeEnabled();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Reativar o robô' }));

    const reauth = await screen.findByRole('alertdialog', { name: 'Confirme que é você' });
    await userEvent.type(
      within(reauth).getByLabelText('Código do aplicativo autenticador'),
      '123456',
    );
    await userEvent.click(within(reauth).getByRole('button', { name: 'Confirmar' }));

    expect(await screen.findByText('Reativação enviada ao robô')).toBeInTheDocument();
    const calls = api.find('POST', '/control/reactivate');
    expect(calls).toHaveLength(2);
    expect(calls[1]?.body).toMatchObject({
      operator_name: 'ana.silva',
      reason: 'revisei o ocorrido',
    });
  });

  it('Dado_Pausar_Quando_CliqueDuplo_Entao_UmSoPedido', async () => {
    const api = stubApi({
      'GET /status': () => makeStatus({ state: 'RUNNING' }),
      'POST /control/pause': () => accepted({ state: 'pending_delivery' }),
    });
    renderPage(<ControlePage />);
    await userEvent.click(await screen.findByRole('button', { name: /Pausar novas entradas/ }));
    const dialog = await fillOperatorDialog(/Pausar novas entradas\?/);
    const go = within(dialog).getByRole('button', { name: 'Pausar novas entradas' });
    await userEvent.dblClick(go);
    await waitFor(() => expect(api.find('POST', '/control/pause')).toHaveLength(1));
    expect(api.find('POST', '/control/pause')[0]?.body).toMatchObject({
      reason: 'revisei o ocorrido',
    });
  });

  it('Dado_ComandoNaoEntregue_Quando_Pausa_Entao_NaoDizAplicado', async () => {
    stubApi({
      'GET /status': () => makeStatus({ state: 'RUNNING' }),
      'POST /control/pause': () => accepted({ state: 'pending_delivery' }),
    });
    renderPage(<ControlePage />);
    await userEvent.click(await screen.findByRole('button', { name: /Pausar novas entradas/ }));
    const dialog = await fillOperatorDialog(/Pausar novas entradas\?/);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Pausar novas entradas' }));

    expect(await screen.findByText(/Aguardando entrega/)).toBeInTheDocument();
    expect(screen.queryByText(/Aplicado:/)).not.toBeInTheDocument();
  });

  // Contrato: 5xx/timeout/sem resposta NAO provam que o robo nao recebeu o pedido; so 4xx claro permite "Nada mudou".
  it('Dado_FalhaDe5xx_Quando_Pausa_Entao_MantemDialogoEDizNaoConfirmado', async () => {
    stubApi({
      'GET /status': () => makeStatus({ state: 'RUNNING' }),
      'POST /control/pause': () => problem(503, 'Indisponível'),
    });
    renderPage(<ControlePage />);
    await userEvent.click(await screen.findByRole('button', { name: /Pausar novas entradas/ }));
    const dialog = await fillOperatorDialog(/Pausar novas entradas\?/);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Pausar novas entradas' }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent(/Não confirmado/);
    expect(alert).toHaveTextContent(/Histórico de controle/);
    expect(alert).not.toHaveTextContent(/Nada mudou/);
    expect(screen.queryByText('Pausa enviada ao robô')).not.toBeInTheDocument();
  });

  it('Dado_4xxClaro_Quando_Pausa_Entao_AfirmaQueNadaMudou', async () => {
    stubApi({
      'GET /status': () => makeStatus({ state: 'RUNNING' }),
      'POST /control/pause': () => problem(422, 'Inválido'),
    });
    renderPage(<ControlePage />);
    await userEvent.click(await screen.findByRole('button', { name: /Pausar novas entradas/ }));
    const dialog = await fillOperatorDialog(/Pausar novas entradas\?/);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Pausar novas entradas' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/Nada mudou/);
  });
});
