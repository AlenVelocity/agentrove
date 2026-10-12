import { useNavigate } from 'react-router-dom';
import { ChevronRight, Cloud } from 'lucide-react';
import { useOrdinConfigQuery, useOrdinStatusQuery } from '@/hooks/queries/useOrdinQueries';
import { useAuthStore } from '@/store/authStore';
import styles from './SidebarOrdinNudge.module.scss';

// First-run pointer to the Ordin connection. Once connected, Ordin chats sit
// in the regular list as cloud chats and this disappears.
export function SidebarOrdinNudge() {
  const navigate = useNavigate();
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const config = useOrdinConfigQuery();
  const enabled = config.data?.enabled === true;
  const status = useOrdinStatusQuery(enabled && isAuthenticated);

  if (!enabled || !status.data || status.data.connected) return null;

  return (
    <div className={styles.wrap}>
      <button
        type="button"
        className={styles.nudge}
        onClick={() => navigate('/settings', { state: { tab: 'cloud' } })}
      >
        <Cloud className={styles.icon} aria-hidden="true" />
        <span className={styles.text}>
          <span className={styles.title}>Connect Ordin</span>
          <span className={styles.detail}>Your cloud chats, in here</span>
        </span>
        <ChevronRight className={styles.chevron} aria-hidden="true" />
      </button>
    </div>
  );
}
