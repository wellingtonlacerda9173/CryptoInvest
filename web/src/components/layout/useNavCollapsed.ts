import { useCallback, useState } from 'react';

const KEY = 'ci-nav-collapsed';

export function useNavCollapsed() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(KEY) === '1';
    } catch {
      return false;
    }
  });

  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      try {
        localStorage.setItem(KEY, prev ? '0' : '1');
      } catch {
        // sem armazenamento: vale so nesta sessao
      }
      return !prev;
    });
  }, []);

  return [collapsed, toggle] as const;
}
