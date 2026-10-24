import type { Role } from '@/api/schemas';

export interface SessionUser {
  id: string;
  name: string;
  role: Role;
}

export interface Session {
  user: SessionUser;
  /** Fim da janela de reautenticacao recente (acoes sensiveis); o servidor e quem decide. */
  freshAuthUntil: string | null;
}

export type LoginStage = 'totp' | 'enrollment';

export interface EnrollmentStart {
  qrDataUri: string;
  secret: string;
}

export interface EnrollmentResult {
  session: Session;
  recoveryCodes: string[];
}

/** Camada abstrata de autenticacao: a UI so conhece esta interface (cookie de sessao httpOnly + CSRF). */
export interface AuthBackend {
  getSession(): Promise<Session | null>;
  login(username: string, password: string): Promise<{ stage: LoginStage }>;
  verifySecondFactor(input: { code: string } | { recoveryCode: string }): Promise<Session>;
  startEnrollment(activationToken: string): Promise<EnrollmentStart>;
  confirmEnrollment(code: string): Promise<EnrollmentResult>;
  reauth(code: string): Promise<Session>;
  logout(): Promise<void>;
}
