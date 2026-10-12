import { useCallback, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Loader2, ArrowRight, ExternalLink } from 'lucide-react';
import { Button } from '@/components/ui/primitives/Button/Button';
import { FieldMessage } from '@/components/ui/primitives/FieldMessage/FieldMessage';
import { Input } from '@/components/ui/primitives/Input/Input';
import { Label } from '@/components/ui/primitives/Label/Label';
import { useAuthStore } from '@/store/authStore';
import { useLoginMutation } from '@/hooks/queries/useAuthQueries';
import { useAuthForm } from '@/hooks/useAuthForm';
import { usePortalSignIn } from '@/hooks/usePortalSignIn';
import { portalAuthService } from '@/services/portalAuthService';
import { isValidEmail } from '@/utils/validation';
import { AuthPageLayout } from '@/components/auth/AuthPageLayout/AuthPageLayout';
import { AuthErrorBanner } from '@/components/auth/AuthErrorBanner/AuthErrorBanner';
import styles from './LoginPage.module.scss';

type LoginFormData = {
  email: string;
  password: string;
};

type LoginFormErrors = Partial<Record<keyof LoginFormData, string>>;

const validateForm = (values: LoginFormData): LoginFormErrors | null => {
  const errors: LoginFormErrors = {};

  if (!values.email) {
    errors.email = 'Email is required';
  } else if (!isValidEmail(values.email)) {
    errors.email = 'Invalid email address';
  }

  if (!values.password) {
    errors.password = 'Password is required';
  }

  return Object.keys(errors).length ? errors : null;
};

const getFieldConfigs = (
  onForgotPassword: () => void,
): Array<{
  name: keyof LoginFormData;
  label: string;
  placeholder: string;
  type: 'email' | 'password';
  action?: ReactNode;
}> => [
  {
    name: 'email',
    label: 'Email address',
    placeholder: 'name@example.com',
    type: 'email',
  },
  {
    name: 'password',
    label: 'Password',
    placeholder: 'Enter your password',
    type: 'password',
    action: (
      <Button
        type="button"
        variant="link"
        className={styles['link-small']}
        onClick={onForgotPassword}
      >
        Forgot password?
      </Button>
    ),
  },
];

export function LoginPage() {
  const navigate = useNavigate();
  const [showPasswordForm, setShowPasswordForm] = useState(false);

  const finishSignIn = useCallback(() => {
    useAuthStore.getState().setAuthenticated(true);
    navigate('/');
  }, [navigate]);

  // The sidecar says whether the portal sign-in is configured; until it answers,
  // or when it is off, the password form is the only option.
  const portalConfig = useQuery({
    queryKey: ['auth', 'portal-config'],
    queryFn: () => portalAuthService.getConfig(),
    staleTime: Infinity,
    retry: false,
  });
  const portalEnabled = portalConfig.data?.enabled === true;
  const portal = usePortalSignIn({ onSuccess: finishSignIn });

  const loginMutation = useLoginMutation({
    onSuccess: finishSignIn,
  });

  const { values, errors, setErrors, handleChange } = useAuthForm<LoginFormData>({
    email: '',
    password: '',
  });

  const handleForgotPassword = () => {
    navigate('/forgot-password');
  };

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();

      const validationErrors = validateForm(values);
      if (validationErrors) {
        setErrors(validationErrors);
        return;
      }

      setErrors(null);
      const attemptValues = { ...values };
      loginMutation.mutate(
        {
          username: attemptValues.email,
          password: attemptValues.password,
        },
        {
          onError: (error) => {
            if (error.message.includes('Email not verified')) {
              sessionStorage.setItem('pending_verification_email', attemptValues.email);
              navigate('/verify-email');
            }
          },
        },
      );
    },
    [loginMutation, navigate, setErrors, values],
  );

  const title = 'Welcome to Agentrove';
  const subtitle = portalEnabled
    ? 'Sign in with your Extraordinary account'
    : 'Sign in to continue to your account';

  const isSubmitting = loginMutation.isPending;
  const error = loginMutation.error?.message;
  const fieldConfigs = getFieldConfigs(handleForgotPassword);
  const passwordFormVisible = !portalEnabled || showPasswordForm;
  const portalWaiting = portal.phase === 'waiting';

  return (
    <AuthPageLayout title={title} subtitle={subtitle}>
      {portalEnabled && (
        <div className={styles.portal}>
          {portal.error && (
            <AuthErrorBanner>
              <p className={styles['error-text']}>{portal.error}</p>
            </AuthErrorBanner>
          )}
          <Button
            type="button"
            variant="primary"
            size="lg"
            className={styles['portal-button']}
            onClick={() => void portal.signIn()}
            disabled={portalWaiting}
            isLoading={portal.phase === 'starting'}
            loadingText="Opening your browser..."
            loadingIcon={<Loader2 className={styles['spinner-icon']} />}
          >
            <span>{portalWaiting ? 'Waiting for your browser' : 'Sign in with Extraordinary'}</span>
            <ExternalLink className={styles.icon} />
          </Button>
          {portalWaiting && (
            <p className={styles['portal-hint']}>
              Finish signing in in the browser window, then come back here.{' '}
              <Button
                type="button"
                variant="link"
                className={styles['link-small']}
                onClick={portal.cancel}
              >
                Cancel
              </Button>
            </p>
          )}
          <div className={styles.footer}>
            <Button
              type="button"
              variant="link"
              className={styles['link-small']}
              onClick={() => setShowPasswordForm((visible) => !visible)}
            >
              {showPasswordForm ? 'Hide email and password' : 'Use email and password instead'}
            </Button>
          </div>
        </div>
      )}

      {passwordFormVisible && (
        <form onSubmit={handleSubmit} className={styles.form}>
          {error && (
            <AuthErrorBanner>
              <p className={styles['error-text']}>{error}</p>
            </AuthErrorBanner>
          )}

          <div className={styles.fields}>
            {fieldConfigs.map(({ name, label, placeholder, type, action }) => (
              <div key={name} className={styles.field}>
                <div className={styles['field-header']}>
                  <Label htmlFor={name} className={styles.label}>
                    {label}
                  </Label>
                  {action}
                </div>
                <Input
                  id={name}
                  type={type}
                  value={values[name]}
                  onChange={(e) => handleChange(name, e.target.value)}
                  placeholder={placeholder}
                  autoComplete={type === 'password' ? 'current-password' : 'email'}
                  hasError={Boolean(errors?.[name])}
                />
                <FieldMessage variant="error">{errors?.[name]}</FieldMessage>
              </div>
            ))}
          </div>

          <Button
            type="submit"
            variant="primary"
            size="lg"
            className={styles.submit}
            isLoading={isSubmitting}
            loadingText="Signing in..."
            loadingIcon={<Loader2 className={styles['spinner-icon']} />}
          >
            <span>Sign in</span>
            <ArrowRight className={styles.icon} />
          </Button>
        </form>
      )}

      {passwordFormVisible && (
        <div className={styles.footer}>
          <Button
            type="button"
            variant="link"
            className={styles['link-small']}
            onClick={() => navigate('/signup')}
          >
            Don{'’'}t have an account? Create one
          </Button>
        </div>
      )}
    </AuthPageLayout>
  );
}
