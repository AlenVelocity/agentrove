import { apiClient } from '@/lib/api';
import { ensureResponse, serviceCall } from '@/services/base/BaseService';
import { authStorage } from '@/utils/storage';

export interface PortalAuthConfig {
  enabled: boolean;
  issuer: string | null;
}

interface PortalLoginStart {
  state: string;
  authorize_url: string;
}

export interface PortalLoginStatus {
  status: 'pending' | 'complete' | 'error';
  access_token?: string | null;
  refresh_token?: string | null;
  detail?: string | null;
}

async function getConfig(): Promise<PortalAuthConfig> {
  return serviceCall(async () => {
    const response = await apiClient.get<PortalAuthConfig>('/auth/portal/config');
    return ensureResponse(response, 'Invalid response from server');
  });
}

async function start(): Promise<PortalLoginStart> {
  return serviceCall(async () => {
    const response = await apiClient.post<PortalLoginStart>('/auth/portal/start');
    return ensureResponse(response, 'Invalid response from server');
  });
}

async function poll(state: string): Promise<PortalLoginStatus> {
  const response = await apiClient.get<PortalLoginStatus>(
    `/auth/portal/poll?state=${encodeURIComponent(state)}`,
  );
  return ensureResponse(response, 'Invalid response from server');
}

// Stores the local session the sidecar minted once the portal sign-in finished.
async function acceptTokens(status: PortalLoginStatus): Promise<boolean> {
  if (status.status !== 'complete' || !status.access_token || !status.refresh_token) {
    return false;
  }
  await authStorage.setTokens(status.access_token, status.refresh_token);
  return true;
}

// Drops the portal grant behind the local session; the local logout follows.
async function logout(): Promise<void> {
  try {
    await apiClient.post('/auth/portal/logout');
  } catch {
    // Not signed in with the portal, or the sidecar is already gone.
  }
}

export const portalAuthService = {
  getConfig,
  start,
  poll,
  acceptTokens,
  logout,
};
