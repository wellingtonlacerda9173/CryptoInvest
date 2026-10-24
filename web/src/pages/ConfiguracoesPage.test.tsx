import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { makeCredential as baseCredential } from '@/mocks/mockState';
import {
  DECREASE_CHANGE,
  INCREASE_CHANGE,
  makeFullSettings,
  savedResult,
  validation,
} from '@/test/settingsData';
import { FRESH_REQUIRED, problem, renderPage, stubApi, type Routes_ } from '@/test/render';
import ConfiguracoesPage from './ConfiguracoesPage';

const makeCredential = (over: Partial<ReturnType<typeof baseCredential>> = {}) => ({
  ...baseCredential(),
  ...over,
});
const ready = () => screen.findByRole('tab', { name: 'Pares' });
const history = { items: [], next_cursor: null };
const base = (over: Routes_ = {}): Routes_ => ({
  'GET /settings': () => makeFullSettings(),
  'GET /settings/history': () => history,
  'GET /credentials': () => [makeCredential()],
  ...over,
});

const panel = () => {
  const el = document.querySelector<HTMLElement>('[role="tabpanel"][data-state="active"]');
  if (!el) throw new Error('sem aba ativa');
  return within(el);
};

const setRisk = async (text: string) => {
  const input = await panel().findByLabelText('Risco por operação (%)');
  await userEvent.clear(input);
  await userEvent.type(input, text);
};

const conflict = (detail: string) =>
  problem(409, 'Conflito', detail, 'https://cryptoinvest.local/problems/conflict');

describe('Configuracoes', () => {
  it('abas, tetos rigidos do servidor e nada para salvar sem mudanca', async () => {
    stubApi(base());
    renderPage(<ConfiguracoesPage />, { path: '/?aba=risco' });
    await ready();
    expect(await screen.findByRole('tab', { name: 'Conta e chaves' })).toBeInTheDocument();
    for (const t of ['Pares', 'Estratégias', 'Risco', 'IA'])
      expect(screen.getByRole('tab', { name: t })).toBeInTheDocument();
    expect(await panel().findByText('Risco por operação: no máximo 1,00%')).toBeInTheDocument();
    expect(panel().getByLabelText('Risco por operação (%)')).toHaveValue('0.5');
    expect(
      panel().getAllByText(/Faixa: de 0,01% a 1% \(teto fixo de segurança\)/)[0],
    ).toBeInTheDocument();
    expect(panel().getByRole('button', { name: 'Nada para salvar' })).toBeDisabled();
  });

  it('IA: texto de que so explica e resume', async () => {
    stubApi(base());
    renderPage(<ConfiguracoesPage />, { path: '/?aba=ia' });
    await ready();
    expect(await panel().findByText('A IA só explica e resume.')).toBeInTheDocument();
    expect(panel().getByRole('switch', { name: /Assistente de IA/ })).not.toBeChecked();
  });

  it('reducao de risco: valida em tempo real, nao pede confirmacao extra e mostra "aplicado"', async () => {
    const api = stubApi(
      base({
        'POST /settings/validate': () => validation([DECREASE_CHANGE]),
        'PUT /settings/risk': () => savedResult(DECREASE_CHANGE),
      }),
    );
    renderPage(<ConfiguracoesPage />, { path: '/?aba=risco' });
    await ready();
    await setRisk('0.25');
    expect(await panel().findByText(/Vale agora/)).toBeInTheDocument();
    const [check] = api.find('POST', '/settings/validate');
    expect(check?.body).toMatchObject({ section: 'risk', value: { risk_per_trade: '0.0025' } });
    expect(panel().queryByText(/Isto aumenta o risco/)).not.toBeInTheDocument();

    await userEvent.click(panel().getByRole('button', { name: 'Salvar alterações' }));
    expect(await screen.findByText('Versão 4 salva.')).toBeInTheDocument();
    const [put] = api.find('PUT', '/settings/risk');
    expect(put?.body).toMatchObject({
      reason: 'Ajuste do risco',
      expected_version: 3,
      acknowledge_risk_increase: false,
      value: { risk_per_trade: '0.0025' },
    });
    expect(await screen.findByText('Aplicado ao robô')).toBeInTheDocument();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('aumento de risco: aviso com antes/agora/custo, segunda confirmacao e reautenticacao', async () => {
    let puts = 0;
    const api = stubApi(
      base({
        'POST /settings/validate': () => validation([INCREASE_CHANGE]),
        'PUT /settings/risk': () => {
          puts += 1;
          return puts === 1 ? FRESH_REQUIRED() : savedResult(INCREASE_CHANGE);
        },
      }),
    );
    renderPage(<ConfiguracoesPage />, { path: '/?aba=risco' });
    await ready();
    await setRisk('0.9');
    const warning = await panel().findByRole('alert');
    expect(warning).toHaveTextContent('Isto aumenta o risco.');
    expect(warning).toHaveTextContent('Antes: 0,50%. Agora: 0,90%.');
    expect(warning).toHaveTextContent('cerca de 90,00 USDT');
    expect(panel().getByText(/Só vale no próximo início do robô/)).toBeInTheDocument();

    await userEvent.click(panel().getByRole('button', { name: 'Salvar alterações' }));
    const confirm = await screen.findByRole('alertdialog', { name: /Confirmar aumento de risco/ });
    const go = within(confirm).getByRole('button', { name: 'Confirmar e salvar' });
    expect(go).toBeDisabled();
    expect(api.find('PUT', '/settings/risk')).toHaveLength(0);
    await userEvent.click(within(confirm).getByRole('checkbox'));
    await userEvent.click(go);

    const reauth = await screen.findByRole('alertdialog', { name: 'Confirme que é você' });
    await userEvent.type(
      within(reauth).getByLabelText('Código do aplicativo autenticador'),
      '123456',
    );
    await userEvent.click(within(reauth).getByRole('button', { name: 'Confirmar' }));

    expect(await screen.findByText('Versão 4 salva.')).toBeInTheDocument();
    const puts_ = api.find('PUT', '/settings/risk');
    expect(puts_).toHaveLength(2);
    expect(puts_[1]?.body).toMatchObject({ acknowledge_risk_increase: true });
    expect(await screen.findByText('Vale no próximo início')).toBeInTheDocument();
  });

  it('erro por campo vindo do endpoint de validacao bloqueia o salvar', async () => {
    stubApi(
      base({
        'POST /settings/validate': () =>
          validation(
            [],
            [
              {
                path: 'risk.risk_per_trade',
                message: 'O valor máximo permitido é 0.01 (teto de segurança do servidor).',
              },
            ],
          ),
      }),
    );
    renderPage(<ConfiguracoesPage />, { path: '/?aba=risco' });
    await ready();
    await setRisk('5');
    expect(await panel().findByText(/O valor máximo permitido é 0.01/)).toBeInTheDocument();
    expect(panel().getByLabelText('Risco por operação (%)')).toBeInvalid();
    expect(panel().getByRole('button', { name: 'Salvar alterações' })).toBeDisabled();
  });

  it('texto que nao e numero e recusado no proprio campo, sem ir ao servidor', async () => {
    const api = stubApi(base());
    renderPage(<ConfiguracoesPage />, { path: '/?aba=risco' });
    await ready();
    await setRisk('abc');
    expect(await panel().findByText('Digite um número.')).toBeInTheDocument();
    expect(api.find('POST', '/settings/validate')).toHaveLength(0);
  });

  it('409 de versao: avisa, recarrega os valores e nao salva', async () => {
    const api = stubApi(
      base({
        'POST /settings/validate': () => validation([DECREASE_CHANGE]),
        'PUT /settings/risk': () =>
          conflict('A configuração mudou enquanto você editava. Recarregue e tente de novo.'),
      }),
    );
    renderPage(<ConfiguracoesPage />, { path: '/?aba=risco' });
    await ready();
    await setRisk('0.25');
    await panel().findByText(/Vale agora/);
    await userEvent.click(panel().getByRole('button', { name: 'Salvar alterações' }));
    expect(
      await screen.findByText('A configuração mudou enquanto você editava.'),
    ).toBeInTheDocument();
    await waitFor(() => expect(api.find('GET', '/settings').length).toBeGreaterThan(1));
    expect(screen.queryByText(/Versão 4 salva/)).not.toBeInTheDocument();
  });

  it('leitor ve os valores, mas nao edita nem consulta chaves', async () => {
    const api = stubApi(base({ 'GET /settings': () => makeFullSettings({ can_edit: false }) }));
    renderPage(<ConfiguracoesPage />, { role: 'reader', path: '/?aba=risco' });
    await ready();
    expect(await panel().findByText(/acesso de leitor/)).toBeInTheDocument();
    expect(panel().getByLabelText('Risco por operação (%)')).toBeDisabled();
    expect(panel().getByRole('button', { name: 'Voltar ao padrão seguro' })).toBeDisabled();
    await userEvent.click(screen.getByRole('tab', { name: 'Conta e chaves' }));
    expect(await panel().findByText(/só aparecem para administradores/)).toBeInTheDocument();
    expect(api.find('GET', '/credentials')).toHaveLength(0);
    expect(api.find('POST', '/settings/validate')).toHaveLength(0);
  });

  it('historico: ator, motivo, estado de aplicacao e diff em texto simples', async () => {
    stubApi(
      base({
        'GET /settings/history': () => ({
          items: [
            {
              ...savedResult(DECREASE_CHANGE, 5).version,
              application: 'partially_applied',
              application_text: 'O robô aplicou só parte: o resto só vale no próximo início.',
            },
          ],
          next_cursor: null,
        }),
      }),
    );
    renderPage(<ConfiguracoesPage />);
    await ready();
    const list = await screen.findByRole('list', { name: 'Versões da configuração' });
    expect(list).toHaveTextContent('Versão 5');
    expect(list).toHaveTextContent('ana.silva');
    expect(list).toHaveTextContent('Motivo: Ajuste do risco');
    expect(list).toHaveTextContent('Aplicado em parte');
    expect(list).toHaveTextContent('O robô aplicou só parte');
    expect(list).toHaveTextContent('Risco por operação: 0,50% → 0,25% (reduz o risco)');
  });
});

describe('Conta e chaves', () => {
  it('lista so metadados, com aviso de saque e de IP, e remover com robo ligado mostra a mensagem da API', async () => {
    const ROBOT = 'Desligue o robô antes de remover a chave: com ele ligado a remoção é recusada.';
    stubApi(
      base({
        'GET /credentials': () => [
          makeCredential({
            warnings: ['A chave não tem restrição de IP.'],
            permissions: {
              can_read: true,
              can_trade: true,
              can_transfer: false,
              can_withdraw: false,
              futures_enabled: false,
              margin_enabled: false,
              ip_restricted: false,
            },
          }),
        ],
        'DELETE /credentials/00000000-0000-4000-8000-0000000000c1': () => conflict(ROBOT),
      }),
    );
    renderPage(<ConfiguracoesPage />, { path: '/?aba=chaves' });
    await ready();
    const list = await panel().findByRole('list', { name: 'Chaves da Binance' });
    expect(list).toHaveTextContent('Saque desligado');
    expect(list).toHaveTextContent('Sem restrição de IP');
    expect(list).toHaveTextContent('A chave não tem restrição de IP.');
    expect(panel().getByText(/permissão de saque é recusada/)).toBeInTheDocument();

    await userEvent.click(within(list).getByRole('button', { name: 'Remover' }));
    const dialog = await screen.findByRole('alertdialog', { name: /Remover a chave/ });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Remover a chave' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(ROBOT);
  });

  it('adicionar: segredo nunca volta na tela e a recusa da API aparece', async () => {
    const api = stubApi(
      base({
        'POST /credentials': () =>
          problem(422, 'Chave recusada', 'Esta chave pode sacar dinheiro e foi recusada.'),
      }),
    );
    renderPage(<ConfiguracoesPage />, { path: '/?aba=chaves' });
    await ready();
    await userEvent.type(
      await panel().findByLabelText('Chave da API (API Key)'),
      'chavePublica123',
    );
    await userEvent.type(panel().getByLabelText('Segredo (Secret)'), 'segredoSuperSecreto987');
    await userEvent.click(panel().getByRole('button', { name: 'Testar e salvar a chave' }));
    expect(await panel().findByText(/pode sacar dinheiro e foi recusada/)).toBeInTheDocument();
    const [post] = api.find('POST', '/credentials');
    expect(post?.body).toMatchObject({ api_secret: 'segredoSuperSecreto987' });
    expect(panel().getByLabelText('Segredo (Secret)')).toHaveValue('');
    expect(document.body.textContent).not.toContain('segredoSuperSecreto987');
  });
});
