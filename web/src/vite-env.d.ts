/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_MOCK?: '0' | '1';
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
