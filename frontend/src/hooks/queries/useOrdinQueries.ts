import { useQuery } from '@tanstack/react-query';
import { ordinCloudService } from '@/services/ordinCloudService';
import type { OrdinCloudStatus } from '@/services/ordinCloudService';
import { queryKeys } from '@/hooks/queries/queryKeys';
import { createMutation } from '@/hooks/queries/createMutation';

export const useOrdinConfigQuery = () =>
  useQuery({
    queryKey: queryKeys.ordin.config,
    queryFn: () => ordinCloudService.getConfig(),
    staleTime: Infinity,
    retry: false,
  });

export const useOrdinStatusQuery = (enabled: boolean) =>
  useQuery({
    queryKey: queryKeys.ordin.status,
    queryFn: () => ordinCloudService.getStatus(),
    enabled,
    staleTime: 30_000,
    retry: false,
  });

// Cloud chats change without this app knowing, so poll gently while shown.
export const useOrdinChatsQuery = (enabled: boolean) =>
  useQuery({
    queryKey: queryKeys.ordin.chats,
    queryFn: () => ordinCloudService.listChats(),
    enabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
    retry: false,
  });

interface OrdinConnectData {
  email: string;
  password: string;
}

export const useOrdinConnectMutation = createMutation<OrdinCloudStatus, Error, OrdinConnectData>(
  (data) => ordinCloudService.connect(data.email, data.password),
  async (queryClient, status) => {
    queryClient.setQueryData(queryKeys.ordin.status, status);
    await queryClient.invalidateQueries({ queryKey: queryKeys.ordin.chats });
  },
);

export const useOrdinDisconnectMutation = createMutation<void, Error, void>(
  () => ordinCloudService.disconnect(),
  (queryClient) => {
    queryClient.setQueryData<OrdinCloudStatus>(queryKeys.ordin.status, {
      connected: false,
      email: null,
    });
    queryClient.removeQueries({ queryKey: queryKeys.ordin.chats });
  },
);
