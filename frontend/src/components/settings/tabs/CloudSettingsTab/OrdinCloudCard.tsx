import { useState, type FormEvent } from 'react';
import toast from 'react-hot-toast';
import clsx from 'clsx';
import { Button } from '@/components/ui/primitives/Button/Button';
import { FieldMessage } from '@/components/ui/primitives/FieldMessage/FieldMessage';
import { Input } from '@/components/ui/primitives/Input/Input';
import { Label } from '@/components/ui/primitives/Label/Label';
import { useAuthStore } from '@/store/authStore';
import {
  useOrdinChatsQuery,
  useOrdinConfigQuery,
  useOrdinConnectMutation,
  useOrdinDisconnectMutation,
  useOrdinStatusQuery,
} from '@/hooks/queries/useOrdinQueries';
import styles from './CloudSettingsTab.module.scss';

function StatusPill({ connected }: { connected: boolean }) {
  return (
    <span className={styles['status-pill']}>
      <span
        className={clsx(styles.dot, connected ? styles['dot--connected'] : styles['dot--offline'])}
      />
      {connected ? 'Connected' : 'Not connected'}
    </span>
  );
}

// Ordin is the team's cloud: its chats run there and show up in the sidebar as
// cloud chats through the sidecar's Ordin proxy. Nothing is rendered when the
// sidecar has no Ordin URLs.
export function OrdinCloudCard() {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const config = useOrdinConfigQuery();
  const enabled = config.data?.enabled === true;
  const status = useOrdinStatusQuery(enabled && isAuthenticated);
  const connected = status.data?.connected === true;
  const chats = useOrdinChatsQuery(connected);
  const connectMutation = useOrdinConnectMutation();
  const disconnectMutation = useOrdinDisconnectMutation();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [formError, setFormError] = useState<string | null>(null);

  if (!enabled) return null;

  const handleConnect = (event: FormEvent) => {
    event.preventDefault();
    const trimmedEmail = email.trim();
    if (!trimmedEmail || !password) {
      setFormError('Enter the email and password you use for Ordin.');
      return;
    }
    setFormError(null);
    connectMutation.mutate(
      { email: trimmedEmail, password },
      {
        onSuccess: (result) => {
          setPassword('');
          toast.success(`Connected to Ordin as ${result.email ?? trimmedEmail}`);
        },
        onError: (error) => setFormError(error.message),
      },
    );
  };

  const handleDisconnect = () => {
    disconnectMutation.mutate(undefined, {
      onSuccess: () => toast.success('Disconnected from Ordin'),
      onError: (error) => toast.error(error.message),
    });
  };

  if (connected) {
    return (
      <div className={styles.card}>
        <div className={styles['instance-header']}>
          <h2 className={styles['instance-title']}>Ordin</h2>
          <StatusPill connected />
        </div>
        <p className={styles['signed-in']}>
          Signed in as <span className={styles.email}>{status.data?.email}</span>. Your Ordin chats
          are in the sidebar with the cloud badge and continue from here.
        </p>
        <div className={styles.stats}>
          <div className={styles['stat-tile']}>
            <div className={styles['stat-value']}>{chats.data?.total ?? '—'}</div>
            <div className={styles['stat-label']}>Chats in Ordin</div>
          </div>
        </div>
        <div className={styles.actions}>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={handleDisconnect}
            isLoading={disconnectMutation.isPending}
            loadingText="Disconnecting..."
          >
            Disconnect
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.card}>
      <div className={styles['instance-header']}>
        <h2 className={styles['instance-title']}>Ordin</h2>
        <StatusPill connected={false} />
      </div>
      <p className={styles['instance-desc']}>
        Connect your Ordin account to see its chats in the sidebar and continue them here. They keep
        running in Ordin.
      </p>
      <form className={styles.form} onSubmit={handleConnect} noValidate>
        <div className={styles.grid}>
          <div>
            <Label htmlFor="ordin-email" className={styles.label}>
              Ordin email
            </Label>
            <Input
              id="ordin-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@extraordinary.com"
              autoComplete="username"
              hasError={Boolean(formError) && !email.trim()}
            />
          </div>
          <div>
            <Label htmlFor="ordin-password" className={styles.label}>
              Password
            </Label>
            <Input
              id="ordin-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Your Ordin password"
              autoComplete="current-password"
              hasError={Boolean(formError) && !password}
            />
          </div>
        </div>
        <FieldMessage variant="error">{formError}</FieldMessage>
        <Button
          type="submit"
          variant="primary"
          size="sm"
          isLoading={connectMutation.isPending}
          loadingText="Connecting..."
        >
          Connect Ordin
        </Button>
      </form>
    </div>
  );
}
