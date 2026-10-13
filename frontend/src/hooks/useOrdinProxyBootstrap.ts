import { useEffect } from 'react';
import { ordinCloudService } from '@/services/ordinCloudService';

// Once signed in, make sure an existing Ordin connection has its cloud
// session, so Ordin chats show up without a visit to Settings.
export function useOrdinProxyBootstrap(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void ordinCloudService
      .getConfig()
      .then((config) => (config.enabled && !cancelled ? ordinCloudService.getStatus() : null))
      .catch((error: unknown) => {
        console.warn('Ordin cloud bootstrap skipped:', error);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
}
