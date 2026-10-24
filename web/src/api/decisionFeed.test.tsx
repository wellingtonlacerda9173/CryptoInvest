import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { makeDecision } from '@/test/marketData';
import { stubApi } from '@/test/render';
import { refreshDecisionFeeds, useDecisionFeed } from './marketQueries';

const summary = { day: '2026-10-24', entries: 1, refusals: 2, signals: 3, waits: 4 };

describe('refreshDecisionFeeds', () => {
  it('evento novo busca so a primeira pagina e junta o inedito ao topo, sem refazer as antigas', async () => {
    let head = [makeDecision(3), makeDecision(2)];
    const api = stubApi({
      'GET /decisions': (_b, url) =>
        url.searchParams.get('cursor') === '2'
          ? { items: [makeDecision(1)], next_cursor: null, summary }
          : { items: head, next_cursor: 2, summary },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useDecisionFeed('paper', {}), { wrapper });
    await waitFor(() => expect(result.current.data).toBeDefined());
    act(() => void result.current.fetchNextPage());
    await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));
    const before = api.calls.length;

    head = [makeDecision(4), makeDecision(3)];
    await act(() => refreshDecisionFeeds(client));

    const ids = () => result.current.data!.pages.flatMap((p) => p.items.map((d) => d.id));
    await waitFor(() => expect(ids()).toEqual([4, 3, 2, 1]));
    expect(api.calls.length - before).toBe(1);
    const pages = result.current.data!.pages;
    expect(pages).toHaveLength(2);
    expect(pages[0]!.next_cursor).toBe(2);
  });
});
