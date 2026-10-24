// Protecao CSRF por double-submit: o servidor emite um cookie legivel (nao httpOnly) e o token
// tambem chega no corpo de /auth/login, /auth/2fa/verify e /auth/me. Mutacoes enviam X-CSRF-Token.
// O cookie de SESSAO e httpOnly e nunca e lido pelo JavaScript.
export const CSRF_HEADER = 'X-CSRF-Token';
const CSRF_COOKIES = ['__Host-ci_csrf', 'ci_csrf'];

let memoryToken: string | null = null;

export const setCsrfToken = (token: string | null) => {
  memoryToken = token;
};

function readCookie(name: string): string | null {
  for (const part of document.cookie.split('; ')) {
    const eq = part.indexOf('=');
    if (eq > 0 && part.slice(0, eq) === name) return decodeURIComponent(part.slice(eq + 1));
  }
  return null;
}

export function getCsrfToken(): string | null {
  for (const name of CSRF_COOKIES) {
    const fromCookie = readCookie(name);
    if (fromCookie) return fromCookie;
  }
  return memoryToken;
}

export const isMutation = (method: string) =>
  !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase());
