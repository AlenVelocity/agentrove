import { useCallback, useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { portalAuthService } from '@/services/portalAuthService';
import { openExternalUrl } from '@/utils/openExternal';

type PortalSignInPhase = 'idle' | 'starting' | 'waiting' | 'error';

interface UsePortalSignInParams {
  onSuccess: () => void;
}

interface UsePortalSignInResult {
  phase: PortalSignInPhase;
  error: string | null;
  signIn: () => Promise<void>;
  // Re-opens the same sign-in page while waiting (the tab got closed or lost).
  openAgain: () => Promise<void>;
  cancel: () => void;
}

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 10 * 60 * 1000;

// Opens the portal's sign-in page in the system browser and waits for the
// sidecar's loopback callback to finish the exchange.
export function usePortalSignIn({ onSuccess }: UsePortalSignInParams): UsePortalSignInResult {
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<PortalSignInPhase>('idle');
  const [error, setError] = useState<string | null>(null);
  const cancelledRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const authorizeUrlRef = useRef<string | null>(null);

  const stopPolling = useCallback(() => {
    cancelledRef.current = true;
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const cancel = useCallback(() => {
    stopPolling();
    authorizeUrlRef.current = null;
    setPhase('idle');
    setError(null);
  }, [stopPolling]);

  const openAgain = useCallback(async () => {
    if (authorizeUrlRef.current) await openExternalUrl(authorizeUrlRef.current);
  }, []);

  const signIn = useCallback(async () => {
    stopPolling();
    cancelledRef.current = false;
    setError(null);
    setPhase('starting');

    let state: string;
    try {
      const started = await portalAuthService.start();
      state = started.state;
      authorizeUrlRef.current = started.authorize_url;
      await openExternalUrl(started.authorize_url);
    } catch (startError) {
      setError(startError instanceof Error ? startError.message : 'Could not start sign-in');
      setPhase('error');
      return;
    }
    if (cancelledRef.current) return;
    setPhase('waiting');

    const deadline = Date.now() + POLL_TIMEOUT_MS;
    const tick = async () => {
      if (cancelledRef.current) return;
      if (Date.now() > deadline) {
        setError('The sign-in took too long. Try again.');
        setPhase('error');
        return;
      }
      try {
        const status = await portalAuthService.poll(state);
        if (cancelledRef.current) return;
        if (status.status === 'error') {
          setError(status.detail ?? 'Sign-in failed');
          setPhase('error');
          return;
        }
        if (await portalAuthService.acceptTokens(status)) {
          await queryClient.cancelQueries();
          queryClient.clear();
          authorizeUrlRef.current = null;
          setPhase('idle');
          onSuccess();
          return;
        }
      } catch {
        // The sidecar is local; a failed poll is transient, keep waiting.
      }
      timerRef.current = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
    };
    timerRef.current = window.setTimeout(() => void tick(), POLL_INTERVAL_MS);
  }, [onSuccess, queryClient, stopPolling]);

  return { phase, error, signIn, openAgain, cancel };
}
