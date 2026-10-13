import { apiClient, setCloudApiBaseUrl } from '@/lib/api';
import { ensureResponse, serviceCall, withAuth } from '@/services/base/BaseService';
import { cloudChatService } from '@/services/cloudChatService';
import { useCloudSettingsStore } from '@/store/cloudSettingsStore';
import { cloudAuthStorage } from '@/utils/storage';

export interface OrdinCloudConfig {
  enabled: boolean;
  web_url: string | null;
}

export interface OrdinCloudStatus {
  connected: boolean;
  email: string | null;
  proxy_url?: string | null;
  proxy_access_token?: string | null;
  proxy_refresh_token?: string | null;
}

export interface OrdinCloudChat {
  id: string;
  title: string;
  case_id: string | null;
  created_at: string;
  updated_at: string;
  pinned_at: string | null;
  web_url: string;
}

export interface OrdinCloudChats {
  items: OrdinCloudChat[];
  page: number;
  per_page: number;
  total: number;
  pages: number;
}

const PROXY_PATH = '/ordin-proxy';

export function isOrdinProxyUrl(url: string): boolean {
  return url.endsWith(PROXY_PATH);
}

async function getConfig(): Promise<OrdinCloudConfig> {
  return serviceCall(async () => {
    const response = await apiClient.get<OrdinCloudConfig>('/ordin/config');
    return ensureResponse(response, 'Invalid response from server');
  });
}

async function enableProxy(status: OrdinCloudStatus, fallbackEmail: string): Promise<void> {
  if (!status.proxy_url || !status.proxy_access_token || !status.proxy_refresh_token) return;
  setCloudApiBaseUrl(status.proxy_url);
  await cloudAuthStorage.setTokens(status.proxy_access_token, status.proxy_refresh_token);
  useCloudSettingsStore.getState().setCloud(status.proxy_url, status.email ?? fallbackEmail);
}

function hasProxySession(): boolean {
  const { cloudUrl } = useCloudSettingsStore.getState();
  return isOrdinProxyUrl(cloudUrl) && !!cloudAuthStorage.getRefreshToken();
}

// Also heals a connection whose cloud session is missing (the app lost it, or
// Ordin was connected before the proxy existed).
async function getStatus(): Promise<OrdinCloudStatus> {
  return withAuth(async () => {
    const response = await apiClient.get<OrdinCloudStatus>('/ordin/status');
    const status = ensureResponse(response, 'Invalid response from server');
    if (status.connected && !hasProxySession()) {
      const session = await apiClient.post<OrdinCloudStatus>('/ordin/proxy-session');
      await enableProxy(ensureResponse(session, 'Invalid response from server'), '');
    }
    return status;
  });
}

// The password goes to the sidecar, which signs in to Ordin once and keeps
// only the tokens. The sidecar then serves the chats through its Ordin proxy,
// which the cloud client is pointed at here.
async function connect(email: string, password: string): Promise<OrdinCloudStatus> {
  return withAuth(async () => {
    const response = await apiClient.post<OrdinCloudStatus>('/ordin/connect', {
      email,
      password,
    });
    const status = ensureResponse(response, 'Invalid response from server');
    await enableProxy(status, email);
    return status;
  });
}

async function disconnect(): Promise<void> {
  if (isOrdinProxyUrl(useCloudSettingsStore.getState().cloudUrl)) {
    cloudChatService.disconnect();
  }
  await withAuth(async () => {
    await apiClient.delete('/ordin/connect');
  });
}

// The sidecar picks a new port on every launch, so a saved proxy URL is only
// right for the port it was saved on.
export function restoreOrdinProxyUrl(port: number): void {
  const { cloudUrl, connectedEmail, setCloud } = useCloudSettingsStore.getState();
  if (!cloudUrl || !isOrdinProxyUrl(cloudUrl)) return;
  const url = `http://127.0.0.1:${port}${PROXY_PATH}`;
  if (url !== cloudUrl) setCloud(url, connectedEmail ?? '');
  setCloudApiBaseUrl(url);
}

async function listChats(page = 1, perPage = 50): Promise<OrdinCloudChats> {
  return withAuth(async () => {
    const response = await apiClient.get<OrdinCloudChats>(
      `/ordin/chats?page=${page}&per_page=${perPage}`,
    );
    return ensureResponse(response, 'Invalid response from server');
  });
}

export const ordinCloudService = {
  getConfig,
  getStatus,
  connect,
  disconnect,
  listChats,
};
