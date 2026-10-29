/**
 * Shared UI primitives.
 *
 * A small, deliberate set — buttons, icon buttons, badges, chips, fields, switches,
 * stats, dialogs, and toasts — so every screen shares the same states and spacing.
 * Styles live in `index.css` under the component layer; this file owns behaviour
 * (accessibility attributes, focus management) rather than visual decisions.
 */

import {
  useCallback,
  useEffect,
  useRef,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";

import { AlertIcon, CheckIcon, CloseIcon } from "./icons";

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md";

export function Button({
  variant = "secondary",
  size = "md",
  className,
  children,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
}) {
  return (
    <button
      type={type}
      className={cx("btn", `btn-${variant}`, size === "sm" && "btn-sm", className)}
      {...rest}
    >
      {children}
    </button>
  );
}

export function IconButton({
  label,
  tone = "default",
  size = "md",
  className,
  children,
  type = "button",
  ...rest
}: Omit<ButtonHTMLAttributes<HTMLButtonElement>, "aria-label"> & {
  label: string;
  tone?: "default" | "danger";
  size?: "sm" | "md";
}) {
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      className={cx(
        "icon-btn",
        tone === "danger" && "icon-btn-danger",
        size === "sm" && "icon-btn-sm",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Badge({
  tone = "neutral",
  className,
  children,
}: {
  tone?: "neutral" | "accent" | "positive" | "warning" | "danger";
  className?: string;
  children: ReactNode;
}) {
  return <span className={cx("badge", `badge-${tone}`, className)}>{children}</span>;
}

export function Chip({
  active = false,
  className,
  children,
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      type={type}
      aria-pressed={active}
      className={cx("chip", className)}
      {...rest}
    >
      {children}
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

export function StatusDot({ tone }: { tone: "neutral" | "accent" | "positive" | "warning" | "danger" }) {
  return <span className={cx("status-dot", `status-dot-${tone}`)} aria-hidden="true" />;
}

export function SectionHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="section-head">
      <div>
        <h2 className="section-title">{title}</h2>
        {description ? <p className="section-desc">{description}</p> : null}
      </div>
      {action ? <div className="section-action">{action}</div> : null}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </div>
  );
}

export function Field({
  label,
  htmlFor,
  hint,
  children,
}: {
  label: string;
  htmlFor?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label className="field-label" htmlFor={htmlFor}>
        {label}
        {hint ? <span className="field-hint">{hint}</span> : null}
      </label>
      {children}
    </div>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className="switch"
      onClick={() => onChange(!checked)}
    >
      <span className="switch-thumb" />
    </button>
  );
}

/**
 * Modal dialog with focus management.
 *
 * Escape is handled in the capture phase and stopped, so the app-wide Escape shortcut
 * (hide the window) does not fire while a dialog is open. Focus moves to the first
 * control on open and returns to the previously focused element on close.
 */
export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    restoreRef.current = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const focusFirst = () => {
      const target = panel?.querySelector<HTMLElement>("[data-autofocus]");
      (target ?? panel)?.focus();
    };
    const timer = window.setTimeout(focusFirst, 0);

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panel) return;
      const nodes = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
        ),
      );
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      window.clearTimeout(timer);
      restoreRef.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="dialog-scrim"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="ss-dialog-title"
        tabIndex={-1}
        className="dialog"
      >
        <div className="dialog-header">
          <h2 id="ss-dialog-title" className="dialog-title">
            {title}
          </h2>
          <IconButton label="Close" size="sm" onClick={onClose}>
            <CloseIcon size={14} />
          </IconButton>
        </div>
        <div className="dialog-body">{children}</div>
        {footer ? <div className="dialog-actions">{footer}</div> : null}
      </div>
    </div>
  );
}

export type ToastTone = "positive" | "danger" | "neutral";

export function Toast({
  message,
  tone = "neutral",
  onDismiss,
}: {
  message: string;
  tone?: ToastTone;
  onDismiss: () => void;
}) {
  useEffect(() => {
    const timer = window.setTimeout(onDismiss, 3600);
    return () => window.clearTimeout(timer);
  }, [message, onDismiss]);

  return (
    <div className={cx("toast", `toast-${tone}`)} role="status" aria-live="polite">
      <span className="toast-icon">
        {tone === "danger" ? <AlertIcon size={14} /> : <CheckIcon size={14} />}
      </span>
      <span className="toast-text">{message}</span>
    </div>
  );
}

/** Stable dismiss handler so the toast timer is not restarted on every render. */
export function useDismiss(setter: (value: null) => void): () => void {
  return useCallback(() => setter(null), [setter]);
}
