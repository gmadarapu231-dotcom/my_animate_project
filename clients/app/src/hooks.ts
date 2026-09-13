/**
 * Data fetching.
 *
 * Hand-rolled rather than pulling in a query library: the app needs
 * loading/error/refetch and nothing more, and a small readable hook is easier
 * to reason about than a cache invalidation policy nobody configured.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, CareerOsClient, Health } from './api';
import { DemoClient, isDemo } from './demo';
import { Settings, defaultBaseUrl, loadSettings, saveSettings } from './settings';

// ---------------------------------------------------------------------------
// Client context
// ---------------------------------------------------------------------------
export type Session = {
  client: CareerOsClient;
  /** True in the published demo build: fixed data, no live model, no writes. */
  demo: boolean;
  settings: Settings;
  ready: boolean;
  health: Health | null;
  healthError: string | null;
  update: (next: Settings) => Promise<void>;
  recheck: () => void;
};

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error('useSession must be used inside <SessionProvider>');
  return session;
}

export function useSessionState(): Session {
  const [settings, setSettings] = useState<Settings>({ baseUrl: defaultBaseUrl(), token: null });
  const [ready, setReady] = useState(false);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (isDemo()) {
      setReady(true);
      return;
    }
    let live = true;
    loadSettings().then((loaded) => {
      if (!live) return;
      setSettings(loaded);
      setReady(true);
    });
    return () => {
      live = false;
    };
  }, []);

  // In the demo build the client is swapped wholesale, so no screen has to
  // branch on demo mode to fetch data.
  const demo = isDemo();
  const client = useMemo(
    () => (demo ? new DemoClient() : new CareerOsClient({ baseUrl: settings.baseUrl, token: settings.token })),
    [demo, settings.baseUrl, settings.token],
  );

  useEffect(() => {
    if (!ready) return;
    let live = true;
    setHealthError(null);
    client
      .health()
      .then((value) => live && setHealth(value))
      .catch((error: ApiError) => {
        if (!live) return;
        setHealth(null);
        setHealthError(error.message);
      });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, settings.baseUrl, settings.token, nonce]);

  const update = useCallback(async (next: Settings) => {
    await saveSettings(next);
    setSettings(next);
  }, []);

  const recheck = useCallback(() => setNonce((n) => n + 1), []);

  return { client, demo, settings, ready, health, healthError, update, recheck };
}

// ---------------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------------
export type Query<T> = {
  data: T | null;
  loading: boolean;
  error: ApiError | null;
  refresh: () => void;
};

/**
 * Run `fetcher` when `deps` change, keeping the previous data visible while a
 * refresh is in flight so the UI does not flash empty.
 */
export function useQuery<T>(fetcher: () => Promise<T>, deps: unknown[] = []): Query<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [nonce, setNonce] = useState(0);
  // Guards against a slow earlier response overwriting a newer one.
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    setLoading(true);
    setError(null);
    fetcher()
      .then((value) => {
        if (generation.current === current) {
          setData(value);
          setLoading(false);
        }
      })
      .catch((err: ApiError) => {
        if (generation.current === current) {
          setError(err);
          setLoading(false);
        }
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);
  return { data, loading, error, refresh };
}

/** An action with pending/error state, for buttons that POST. */
export function useAction<Args extends unknown[], T>(
  run: (...args: Args) => Promise<T>,
): {
  invoke: (...args: Args) => Promise<T | null>;
  pending: boolean;
  error: ApiError | null;
  result: T | null;
  reset: () => void;
} {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<T | null>(null);

  const invoke = useCallback(
    async (...args: Args) => {
      setPending(true);
      setError(null);
      try {
        const value = await run(...args);
        setResult(value);
        return value;
      } catch (err) {
        setError(err as ApiError);
        return null;
      } finally {
        setPending(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const reset = useCallback(() => {
    setError(null);
    setResult(null);
  }, []);

  return { invoke, pending, error, result, reset };
}
