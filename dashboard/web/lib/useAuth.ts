'use client';

import useSWR from 'swr';
import { fetcher, AuthUser } from './api';

export function useAuth() {
  const { data, error, isLoading, mutate } = useSWR<{ user: AuthUser }>(
    '/api/auth/me',
    fetcher,
    {
      shouldRetryOnError: false,
      revalidateOnFocus: false,
    }
  );

  return {
    user: data?.user ?? null,
    isLoading,
    isAuthenticated: !!data?.user && !error,
    error,
    mutate,
  };
}
