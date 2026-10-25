import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import {
  aiUsageSchema,
  aiValueSchema,
  lessonDetailSchema,
  lessonPageSchema,
  lessonRunSchema,
} from './aiSchemas';
import { http } from './http';

export const AI_KEYS = {
  usage: ['ai', 'usage'],
  lessons: ['ai', 'lessons'],
  lesson: (id: string) => ['ai', 'lessons', id],
  value: (days: number) => ['ai', 'value', days],
} as const;

export const useAiUsage = () =>
  useQuery({
    queryKey: AI_KEYS.usage,
    queryFn: ({ signal }) => http('/ai/usage', { schema: aiUsageSchema, signal }),
    staleTime: 0,
  });

export const useAiLessons = () =>
  useInfiniteQuery({
    queryKey: AI_KEYS.lessons,
    queryFn: ({ signal, pageParam }) =>
      http(`/ai/lessons?limit=10${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ''}`, {
        schema: lessonPageSchema,
        signal,
      }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

export const useAiLesson = (id: string | null) =>
  useQuery({
    queryKey: AI_KEYS.lesson(id ?? ''),
    queryFn: ({ signal }) => http(`/ai/lessons/${id}`, { schema: lessonDetailSchema, signal }),
    enabled: id !== null,
  });

export const useAiValue = (days: number) =>
  useQuery({
    queryKey: AI_KEYS.value(days),
    queryFn: ({ signal }) => http(`/ai/value?days=${days}`, { schema: aiValueSchema, signal }),
    staleTime: 0,
  });

export const aiApi = {
  review: (id: string, body: { decision: 'approve' | 'reject'; reason: string }) =>
    http(`/ai/lessons/${id}/review`, { method: 'POST', body, schema: lessonDetailSchema }),
  run: () => http('/ai/lessons/run', { method: 'POST', schema: lessonRunSchema }),
};
