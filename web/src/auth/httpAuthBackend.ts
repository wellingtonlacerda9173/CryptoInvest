import { setCsrfToken } from '@/api/csrf';
import { ApiError, http } from '@/api/http';
import {
  enrollmentDoneSchema,
  enrollmentStartSchema,
  loginStepSchema,
  meSchema,
  sessionStartedSchema,
  type Me,
} from '@/api/schemas';
import type { AuthBackend, Session } from './types';

const fromMe = (me: Me): Session => ({
  user: { id: me.id, name: me.username, role: me.role },
  freshAuthUntil: me.fresh_auth_until ?? null,
});

export const httpAuthBackend: AuthBackend = {
  async getSession() {
    try {
      const me = await http('/auth/me', { schema: meSchema, silent401: true });
      setCsrfToken(me.csrf_token);
      return fromMe(me);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      throw error;
    }
  },

  async login(username, password) {
    const step = await http('/auth/login', {
      method: 'POST',
      body: { username, password },
      schema: loginStepSchema,
      silent401: true,
    });
    setCsrfToken(step.csrf_token);
    return { stage: step.stage };
  },

  async verifySecondFactor(input) {
    const body = 'code' in input ? { code: input.code } : { recovery_code: input.recoveryCode };
    const started = await http('/auth/2fa/verify', {
      method: 'POST',
      body,
      schema: sessionStartedSchema,
      silent401: true,
    });
    setCsrfToken(started.csrf_token);
    return {
      user: { id: started.user.id, name: started.user.username, role: started.user.role },
      freshAuthUntil: null,
    };
  },

  async startEnrollment(activationToken) {
    const out = await http('/auth/2fa/enroll', {
      method: 'POST',
      body: { activation_token: activationToken },
      schema: enrollmentStartSchema,
      silent401: true,
    });
    return { qrDataUri: out.qr_svg_data_uri, secret: out.secret };
  },

  async confirmEnrollment(code) {
    const done = await http('/auth/2fa/enroll/confirm', {
      method: 'POST',
      body: { code },
      schema: enrollmentDoneSchema,
      silent401: true,
    });
    setCsrfToken(done.csrf_token);
    return {
      session: {
        user: { id: done.user.id, name: done.user.username, role: done.user.role },
        freshAuthUntil: null,
      },
      recoveryCodes: done.recovery_codes,
    };
  },

  async reauth(code) {
    const me = await http('/auth/reauth', {
      method: 'POST',
      body: { code },
      schema: meSchema,
    });
    setCsrfToken(me.csrf_token);
    return fromMe(me);
  },

  async logout() {
    try {
      await http('/auth/logout', { method: 'POST' });
    } finally {
      setCsrfToken(null);
    }
  },
};
