import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { mkdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { STATE_FILE } from './global-setup';
import { Totp } from './totp';

const stack = JSON.parse(readFileSync(STATE_FILE, 'utf8')) as {
  username: string;
  password: string;
  activation: string;
};
const BINANCE_KEY = 'e2eKeyPublic1234567890';
const BINANCE_SECRET = 'e2eSecretNeverEchoed9876543210';

let totp: Totp;
let page: Page;

async function fillCode(page: Page, label: string) {
  await page.getByLabel(label).fill(await totp.next());
}

async function reauthIfAsked(page: Page) {
  const dialog = page.getByRole('alertdialog', { name: 'Confirme que é você' });
  const asked = await dialog.waitFor({ timeout: 2_000 }).then(
    () => true,
    () => false,
  );
  if (!asked) return;
  await fillCode(page, 'Código do aplicativo autenticador');
  await dialog.getByRole('button', { name: /Confirmar/ }).click();
  await expect(dialog).toHaveCount(0);
}

async function expectNoSevereAxe(page: Page) {
  // deixa terminar as transicoes de cor (120 ms): axe no meio delas mede um contraste intermediario
  await page.waitForTimeout(300);
  const { violations } = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze();
  expect(violations.filter((v) => v.impact === 'critical' || v.impact === 'serious')).toEqual([]);
}

const SHOTS = resolve(import.meta.dirname, '../../docs/design/app-screenshots');
const WIDTHS = { 1440: 900, 820: 1180, 390: 844 } as const;

/** Capturas do app REAL (API de verdade) nas 3 larguras e 2 temas, para comparar com os mockups. */
async function shoot(target: Page, name: string, ready: () => Promise<void>) {
  mkdirSync(SHOTS, { recursive: true });
  for (const [theme, stored] of [
    ['escuro', 'dark'],
    ['claro', 'light'],
  ] as const) {
    await target.evaluate((t) => localStorage.setItem('ci-theme', t), stored);
    await target.reload();
    await ready();
    for (const [width, height] of Object.entries(WIDTHS)) {
      await target.setViewportSize({ width: Number(width), height });
      await expect(target.locator('[aria-busy="true"]')).toHaveCount(0);
      await target.screenshot({
        path: resolve(SHOTS, `${name}-${theme}-${width}.png`),
        fullPage: true,
      });
    }
  }
  await target.evaluate(() => localStorage.setItem('ci-theme', 'dark'));
  await target.setViewportSize({ width: 1440, height: 900 });
}

test.describe.configure({ mode: 'serial' });

// Uma unica pagina (e cookie de sessao) ao longo do fluxo, como um usuario de verdade.
test.beforeAll(async ({ browser }) => {
  const context = await browser.newContext({
    baseURL: 'http://127.0.0.1:5198',
    locale: 'pt-BR',
  });
  page = await context.newPage();
});

test.afterAll(async () => {
  await page.context().close();
});

test('enrollment do 2FA com token de ativacao e codigos de recuperacao', async () => {
  await page.goto('/dashboard');
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Usuário').fill(stack.username);
  await page.getByLabel('Senha', { exact: true }).fill(stack.password);
  await page.getByRole('button', { name: 'Continuar' }).click();

  await page.getByLabel('Código de ativação').fill(stack.activation);
  await page.getByRole('button', { name: 'Continuar' }).click();

  await expect(
    page.getByRole('img', { name: 'Código QR para o aplicativo autenticador' }),
  ).toBeVisible();
  await page.getByText('Não consegue escanear?').click();
  const secret = (await page.locator('details code').innerText()).trim();
  totp = new Totp(secret);
  await expectNoSevereAxe(page);
  await fillCode(page, 'Código do aplicativo autenticador');

  await expect(
    page.getByRole('heading', { name: 'Guarde seus códigos de recuperação' }),
  ).toBeVisible();
  await expect(
    page.getByRole('list', { name: 'Códigos de recuperação' }).getByRole('listitem'),
  ).toHaveCount(10);
  const continuar = page.getByRole('button', { name: 'Continuar' });
  await expect(continuar).toBeDisabled();
  await page.getByLabel('Guardei os códigos em lugar seguro.').check();
  await continuar.click();
  await expect(page).toHaveURL(/\/(onboarding|dashboard)$/);
});

test('onboarding: chave Binance nunca ecoa o segredo, risco e inicio no Paper', async () => {
  await page.goto('/onboarding');
  await expect(page.getByRole('heading', { name: 'Conectar a Binance' })).toBeVisible();
  await expect(page.getByText(/sacar dinheiro é recusada/).first()).toBeVisible();
  await shoot(page, 'onboarding', () =>
    expect(page.getByRole('heading', { name: 'Conectar a Binance' })).toBeVisible(),
  );
  await page.getByLabel('Chave da API (API Key)').fill(BINANCE_KEY);
  await page.getByLabel('Segredo (Secret)').fill(BINANCE_SECRET);
  await expectNoSevereAxe(page);
  await page.getByRole('button', { name: 'Testar e salvar a chave' }).click();
  await page.getByRole('button', { name: 'Continuar' }).click();

  await expect(page.getByRole('heading', { name: 'Configurar o risco' })).toBeVisible();
  expect(await page.content()).not.toContain(BINANCE_SECRET);
  await page.getByRole('button', { name: 'Continuar' }).click();
  await reauthIfAsked(page);

  await expect(page.getByRole('heading', { name: 'Iniciar no Paper' })).toBeVisible();
  await page.getByRole('button', { name: 'Iniciar no Paper' }).click();
  await reauthIfAsked(page);
  await expect(page).toHaveURL(/\/dashboard$/);
});

test('dashboard com dados reais, F5 mantem a sessao e sem segredos', async () => {
  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();
  await expect(page.getByText(/parado em USDT/i).first()).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText('Operando').first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('region', { name: 'Caminho para o Live' })).toBeVisible();
  await expect(
    page.getByRole('list', { name: 'Critérios para liberar o Live' }).getByRole('listitem').first(),
  ).toBeVisible();
  await shoot(page, 'dashboard', async () => {
    await expect(page.getByRole('region', { name: 'Curva de capital SIMULADO' })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole('region', { name: 'Indicadores principais' })).not.toContainText(
      '—',
    );
  });
  await expectNoSevereAxe(page);
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'Dashboard' })).toBeVisible();
  const html = await page.content();
  expect(html).not.toContain(BINANCE_SECRET);
  expect(html).not.toContain(stack.password);
});

test('controle: pausar, retomar e parar tudo ate o estado aplicado', async () => {
  await page.goto('/controle');
  await expect(page.getByRole('heading', { level: 1, name: 'Controle' })).toBeVisible();
  await expect(page.getByRole('button', { name: /Live ainda não disponível/ })).toBeDisabled();
  await shoot(page, 'controle', () =>
    expect(page.getByRole('heading', { level: 1, name: 'Controle' })).toBeVisible({
      timeout: 30_000,
    }),
  );

  const operate = async (open: RegExp | string, confirm: RegExp | string) => {
    await page.getByRole('button', { name: open }).first().click();
    const dialog = page.getByRole('alertdialog');
    const name = dialog.getByLabel('Seu nome (fica no registro)');
    if (await name.isVisible()) await name.fill('Ana');
    await dialog.getByLabel('Motivo').fill('teste de ponta a ponta');
    await dialog.getByRole('button', { name: confirm }).click();
    await reauthIfAsked(page);
  };

  await operate('Pausar novas entradas', 'Pausar novas entradas');
  await expect(page.getByText('Pausado').first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('aplicado').first()).toBeVisible({ timeout: 30_000 });

  await operate(/^Retomar$/, 'Retomar');
  await expect(page.getByText('Operando').first()).toBeVisible({ timeout: 30_000 });

  await page.getByRole('button', { name: 'Parar tudo' }).first().click();
  const stop = page.getByRole('alertdialog', { name: 'Parar tudo agora?' });
  await stop.getByLabel('Motivo').fill('teste de ponta a ponta');
  await stop.getByRole('button', { name: 'Confirmar: parar tudo' }).click();
  await reauthIfAsked(page);
  await expect(page.getByText(/Parada: |Parada enviada ao robô/).first()).toBeVisible();
  await expectNoSevereAxe(page);
});

test('mercado: velas reais, marcadores, tabela alternativa e par sem velas', async () => {
  await page.goto('/mercado');
  await expect(page.getByRole('heading', { level: 1, name: 'Mercado' })).toBeVisible({
    timeout: 30_000,
  });
  const chart = page.getByRole('img', { name: /Gráfico de velas de BTC\/USDT/ });
  await expect(chart).toBeVisible({ timeout: 30_000 });
  await expect(chart.locator('canvas').first()).toBeVisible();
  const table = page.getByRole('region', { name: 'Tabela das últimas velas' });
  await expect(table.getByRole('row')).toHaveCount(21);
  await expect(page.getByText('Comprado')).toBeVisible();
  await expect(page.getByText(/1 no período mostrado|\d+ no período mostrado/)).toBeVisible();
  await shoot(page, 'mercado', async () => {
    await expect(page.getByRole('img', { name: /Gráfico de velas de BTC/ })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  });
  await expectNoSevereAxe(page);
  await page.getByLabel('Par', { exact: true }).selectOption('ETH/USDT');
  await expect(page.getByText('Ainda sem velas para este par')).toBeVisible();
  await page.getByLabel('Par', { exact: true }).selectOption('BTC/USDT');
});

test('decisoes: timeline real, detalhe com regime, scores e IA, filtro e F5', async () => {
  await page.goto('/decisoes');
  await expect(page.getByRole('heading', { level: 1, name: 'Decisões ao vivo' })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByTestId('resumo-dia')).toContainText('Hoje:', { timeout: 30_000 });
  const first = page
    .getByRole('list', { name: /Decisões do robô/ })
    .getByRole('listitem')
    .first();
  await expect(first).toBeVisible();
  await first.getByRole('button', { name: /Detalhes/ }).click();
  await expect(first).toContainText('tendência de alta');
  await expect(first).toContainText('ema: nota 0,41');
  await expect(first).toContainText('Contexto favorável');
  await expect(first).toContainText('Decisão final');
  await shoot(page, 'decisoes', async () => {
    await expect(page.getByTestId('resumo-dia')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  });
  await expectNoSevereAxe(page);
  await page.getByLabel('Mostrar').selectOption('traded');
  await expect(
    page.getByRole('list', { name: /Decisões do robô/ }).getByRole('listitem'),
  ).toHaveCount(1);
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'Decisões ao vivo' })).toBeVisible({
    timeout: 30_000,
  });
});

test('operacoes: abertas, historico com perdas visiveis, detalhe com grafico e CSV', async () => {
  await page.goto('/operacoes');
  await expect(page.getByRole('heading', { level: 1, name: 'Operações' })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole('region', { name: 'Posições abertas' })).toBeVisible({
    timeout: 30_000,
  });
  await shoot(page, 'operacoes-abertas', async () => {
    await expect(page.getByRole('region', { name: 'Posições abertas' })).toBeVisible({
      timeout: 30_000,
    });
  });
  await page.getByRole('tab', { name: 'Encerradas' }).click();
  const resumo = page.getByTestId('resumo-operacoes');
  await expect(resumo).toContainText('perdas', { timeout: 30_000 });
  const losses = page.locator('tr[data-result="loss"]');
  expect(await losses.count()).toBeGreaterThan(0);
  await expect(losses.first()).toContainText('Perda');
  await expect(losses.first()).toContainText('−');
  await expect(page.getByRole('option', { name: 'Só ganhos' })).toHaveCount(0);

  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Exportar CSV' }).click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/\.csv$/);
  const path = await file.path();
  const { readFileSync: read } = await import('node:fs');
  expect(read(path, 'utf8')).toContain('BTC/USDT');
  await file.delete();

  await shoot(page, 'operacoes-encerradas', async () => {
    await page.getByRole('tab', { name: 'Encerradas' }).click();
    await expect(page.getByTestId('resumo-operacoes')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  });
  await page.getByLabel('Resultado', { exact: true }).selectOption('loss');
  await expect(page.locator('tr[data-result="win"]')).toHaveCount(0);
  await expect(page.locator('tr[data-result="loss"]').first()).toBeVisible();
  await page
    .getByRole('button', { name: /Ver detalhes da operação BTC\/USDT/ })
    .first()
    .click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText('Perda').first()).toBeVisible();
  await expect(dialog.getByRole('img', { name: /no momento da operação/ })).toBeVisible({
    timeout: 30_000,
  });
  await expectNoSevereAxe(page);
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
});

test('desempenho: indicadores reais, amostra pequena, curva e histograma', async () => {
  await page.goto('/desempenho');
  await expect(page.getByRole('heading', { level: 1, name: 'Desempenho' })).toBeVisible({
    timeout: 30_000,
  });
  const kpis = page.getByRole('region', { name: 'Indicadores de desempenho' });
  await expect(kpis).not.toContainText('Carregando');
  await expect(kpis.getByRole('button', { name: 'Profit factor' })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText(/poucas para concluir|Trate como indício/).first()).toBeVisible();
  await expect(
    page.getByRole('region', { name: 'Resultado por estratégia: tabela' }),
  ).toBeVisible();
  await expect(page.getByRole('img', { name: /Distribuição de resultados em R/ })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole('img', { name: /Curva de capital/ })).toBeVisible();
  await shoot(page, 'desempenho', async () => {
    await expect(page.getByRole('img', { name: /Distribuição de resultados em R/ })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  });
  await expectNoSevereAxe(page);
});

const dayIso = (offsetDays: number) =>
  new Date(Date.now() + offsetDays * 86_400_000).toISOString().slice(0, 10);

test('backtest: assistente em etapas ate o relatorio, com candles sinteticos', async () => {
  test.setTimeout(300_000);
  await page.goto('/backtest');
  await expect(page.getByRole('heading', { level: 1, name: 'Backtest' })).toBeVisible();
  await expect(page.getByText('Nenhum backtest ainda')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('heading', { name: 'Etapa 1 de 5: Par' })).toBeVisible();
  await shoot(page, 'backtest-assistente', async () => {
    await expect(page.getByText('Nenhum backtest ainda')).toBeVisible({ timeout: 30_000 });
  });
  await expectNoSevereAxe(page);

  await page.getByRole('checkbox', { name: 'BTC/USDT' }).click();
  await page.getByRole('checkbox', { name: 'SOL/USDT' }).click();
  await page.getByRole('button', { name: /Continuar/ }).click();

  await page.getByRole('radio', { name: 'Datas escolhidas por mim' }).click();
  await page.getByLabel('Data inicial').fill('2017-01-01');
  await page.getByLabel('Data final').fill(dayIso(0));
  await page.getByRole('button', { name: /Continuar/ }).click();
  await expect(page.getByText(/17\/08\/2017/).first()).toBeVisible();
  await page.getByLabel('Data inicial').fill(dayIso(-14));
  await page.getByRole('button', { name: /Continuar/ }).click();

  await page.getByRole('button', { name: /Continuar/ }).click();
  await expect(page.getByRole('heading', { name: 'Etapa 4 de 5: Premissas' })).toBeVisible();
  await expectNoSevereAxe(page);
  await page.getByRole('button', { name: /Continuar/ }).click();
  await expect(page.getByText(/SOL\/USDT/).first()).toBeVisible();
  await page.getByRole('button', { name: 'Rodar backtest' }).click();

  const report = page.getByRole('region', { name: 'Risco e consistência' });
  await expect(
    page.getByRole('progressbar', { name: 'Progresso do backtest' }).or(report),
  ).toBeVisible({ timeout: 60_000 });
  await expect(report).toBeVisible({ timeout: 240_000 });
  await expect(page.getByText('O teste não terminou.')).toHaveCount(0);
  await expect(
    page.getByText('Resultado de simulação no passado. Não garante o futuro.'),
  ).toBeVisible();
  await expect(page.getByRole('region', { name: 'Resultado' })).toBeVisible();
  await expect(page.getByRole('heading', { name: /Premissas e limitações/ })).toBeVisible();
  await expect(page.getByRole('region', { name: 'Operações evitadas', exact: true })).toBeVisible();
  await expect(page.getByText(/Amostra pequena/).first()).toBeVisible();
  await expect(page.getByText(/sem conferência em janelas/)).toBeVisible();
  await expectNoSevereAxe(page);
  const url = page.url();
  await shoot(page, 'backtest-relatorio', async () => {
    await expect(report).toBeVisible({ timeout: 60_000 });
  });
  expect(page.url()).toBe(url);

  await page.getByRole('button', { name: /Novo backtest e histórico/ }).click();
  await expect(page.getByRole('button', { name: /Ver relatório/ }).first()).toBeVisible();
});

test('configuracoes: risco com reducao aplica; com aumento exige confirmacao e reautenticacao', async () => {
  test.setTimeout(240_000);
  await page.goto('/configuracoes?aba=risco');
  const field = page.getByLabel('Risco por operação (%)');
  await expect(field).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Risco por operação: no máximo 1,00%')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Nada para salvar' }).first()).toBeDisabled();
  await shoot(page, 'configuracoes-risco', async () => {
    await expect(page.getByLabel('Risco por operação (%)')).toBeVisible({ timeout: 30_000 });
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  });
  await expectNoSevereAxe(page);

  await field.fill('0.25');
  await expect(page.getByText(/Vale agora/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText('Isto aumenta o risco.')).toHaveCount(0);
  await page.getByRole('button', { name: 'Salvar alterações' }).click();
  await reauthIfAsked(page);
  await expect(page.getByText(/Versão \d+ salva\./)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Aplicado ao robô').first()).toBeVisible({ timeout: 45_000 });

  await expect(page.getByLabel('Risco por operação (%)')).toHaveValue('0.25');
  await page.getByLabel('Risco por operação (%)').fill('0.9');
  const warning = page.getByRole('alert').filter({ hasText: 'Isto aumenta o risco.' });
  await expect(warning).toBeVisible({ timeout: 15_000 });
  await expect(warning).toContainText('Antes: 0,25%. Agora: 0,90%.');
  await expect(warning).toContainText('USDT');
  await page.getByRole('button', { name: 'Salvar alterações' }).click();
  const confirm = page.getByRole('alertdialog', { name: /Confirmar aumento de risco/ });
  await expect(confirm).toBeVisible();
  await expect(confirm.getByRole('button', { name: 'Confirmar e salvar' })).toBeDisabled();
  await confirm.getByRole('checkbox').check();
  await confirm.getByRole('button', { name: 'Confirmar e salvar' }).click();
  await reauthIfAsked(page);
  await expect(page.getByText(/Versão \d+ salva\./)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Vale no próximo início').first()).toBeVisible({ timeout: 30_000 });

  const versions = page.getByRole('list', { name: 'Versões da configuração' });
  await expect(versions).toContainText(stack.username);
  await expect(versions).toContainText('Aumentou o risco');
  await expect(versions).toContainText('Aplicado ao robô');
  await versions
    .getByText(/Ver o que mudou/)
    .first()
    .click();
  await expect(versions).toContainText('Risco por operação: 0,25% → 0,90%');
  await expectNoSevereAxe(page);
});

test('configuracoes: acima do teto rigido e recusado com a regra', async () => {
  await page.goto('/configuracoes?aba=risco');
  const field = page.getByLabel('Risco por operação (%)');
  await expect(field).toBeVisible({ timeout: 30_000 });
  await field.fill('5');
  await expect(page.getByText(/O valor máximo permitido é/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole('button', { name: 'Salvar alterações' })).toBeDisabled();
});

test('configuracoes: chave Binance nova nunca ecoa o segredo', async () => {
  test.setTimeout(180_000);
  await page.goto('/configuracoes?aba=chaves');
  await expect(page.getByRole('list', { name: 'Chaves da Binance' })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText(/permissão de saque é recusada/)).toBeVisible();
  await page.getByLabel('Nome da chave').fill('Reserva');
  await page.getByLabel('Chave da API (API Key)').fill('e2eKeyReserve0987654321');
  await page.getByLabel('Segredo (Secret)').fill(BINANCE_SECRET);
  await page.getByRole('button', { name: 'Testar e salvar a chave' }).click();
  await reauthIfAsked(page);
  const keys = page.getByRole('list', { name: 'Chaves da Binance' });
  await expect(keys).toContainText('Reserva', { timeout: 30_000 });
  await expect(keys).toContainText('Saque desligado');
  await expect(keys).toContainText('IP restrito');
  await expect(page.getByLabel('Segredo (Secret)')).toHaveValue('');
  expect(await page.content()).not.toContain(BINANCE_SECRET);
  await shoot(page, 'configuracoes-chaves', async () => {
    await expect(page.getByRole('list', { name: 'Chaves da Binance' })).toBeVisible({
      timeout: 30_000,
    });
  });
  await expectNoSevereAxe(page);
});

test('licoes da IA: texto como texto, aprovar e rejeitar com motivo e reautenticacao, e valor da IA', async () => {
  await page.goto('/configuracoes?aba=ia');
  await expect(page.getByText('A IA só veta ou reduz a confiança.')).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText('Uso da IA no mês')).toBeVisible();
  await expect(page.getByRole('group', { name: /Gasto da IA no mês/ })).toBeVisible();
  await expectNoSevereAxe(page);

  await page.goto('/configuracoes?aba=licoes');
  const lessons = page.getByRole('list', { name: 'Lições' });
  await expect(lessons.getByRole('listitem')).toHaveCount(2, { timeout: 30_000 });
  const detail = page.getByRole('article', { name: 'Detalhe da lição' });
  // o banco guarda marcacao crua; a API a sanitiza na leitura e o painel exibe so texto
  await expect(detail).toContainText('[removido]elephant[removido]', { timeout: 30_000 });
  await expect(detail).not.toContainText('<b>');
  await expect(detail.locator('b')).toHaveCount(0);
  await expect(detail).toContainText('Evidência: 4 perdas em 6 operações');
  await expect(detail).toContainText('Pendente de revisão');
  await expect(page.getByText(/nunca mudam regras de risco/)).toBeVisible();
  await shoot(page, 'licoes-ia', async () => {
    await expect(page.getByRole('article', { name: 'Detalhe da lição' })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
  });
  await expectNoSevereAxe(page);

  const review = async (label: 'Aprovar' | 'Rejeitar', reason: string) => {
    await page
      .getByRole('article', { name: 'Detalhe da lição' })
      .getByRole('button', { name: label })
      .click();
    const dialog = page.getByRole('alertdialog', { name: new RegExp(`${label} esta lição`) });
    await expect(dialog.getByRole('button', { name: label })).toBeDisabled();
    await dialog.getByLabel(/Motivo/).fill(reason);
    await dialog.getByRole('button', { name: label }).click();
    await reauthIfAsked(page);
    await expect(dialog).toHaveCount(0, { timeout: 30_000 });
  };

  await review('Aprovar', 'confere com o que vimos na semana');
  await expect(detail).toContainText('Aprovada', { timeout: 30_000 });
  await expect(detail).toContainText('confere com o que vimos na semana');
  await expect(detail.getByRole('button', { name: 'Aprovar' })).toBeDisabled();

  await lessons.getByRole('listitem').nth(1).getByRole('button').click();
  await expect(detail).toContainText('Só estatísticas', { timeout: 30_000 });
  await review('Rejeitar', 'sem operações para concluir algo');
  await expect(detail).toContainText('Rejeitada', { timeout: 30_000 });
  await expect(lessons).toContainText('Aprovada');
  await expect(lessons).toContainText('Rejeitada');
  await expectNoSevereAxe(page);

  await page.goto('/configuracoes?aba=valor');
  await expect(page.getByRole('heading', { name: 'Valor da IA' })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Sem atividade da IA neste período')).toBeVisible({
    timeout: 30_000,
  });
  await shoot(page, 'valor-ia', async () => {
    await expect(page.getByText('Sem atividade da IA neste período')).toBeVisible({
      timeout: 30_000,
    });
  });
  await expectNoSevereAxe(page);
});

test('sair e entrar de novo com o segundo fator', async () => {
  await page.goto('/dashboard');
  await page.getByRole('button', { name: 'Sair' }).click();
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Usuário').fill(stack.username);
  await page.getByLabel('Senha', { exact: true }).fill(stack.password);
  await page.getByRole('button', { name: 'Continuar' }).click();
  await fillCode(page, 'Código do aplicativo autenticador');
  await expect(page).toHaveURL(/\/dashboard$/);
});

test('login real nas 3 larguras e 2 temas', async ({ browser }) => {
  const anon = await browser.newContext({ baseURL: 'http://127.0.0.1:5198', locale: 'pt-BR' });
  const fresh = await anon.newPage();
  await fresh.goto('/login');
  await expect(fresh.getByRole('heading', { name: 'Entre para ver o seu robô' })).toBeVisible();
  await shoot(fresh, 'login', () =>
    expect(fresh.getByRole('heading', { name: 'Entre para ver o seu robô' })).toBeVisible(),
  );
  await anon.close();
});
