/**
 * Inline icon set.
 *
 * The UI intentionally has no icon dependency (PLAN.md §4: one static build, two hosts),
 * so these are hand-written 24×24 stroke icons on a shared grid. One family, one stroke
 * weight, one visual language: 1.6px strokes with round caps, sized through `size`.
 */

import type { ReactNode, SVGProps } from "react";

type IconProps = Omit<SVGProps<SVGSVGElement>, "children"> & { size?: number };

function Icon({ size = 16, children, ...rest }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

export function SearchIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </Icon>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09A1.7 1.7 0 0 0 9 19.4a1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4 0v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9c.12.38.4.7.77.9.26.14.55.21.84.22H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0 0-1.51.88Z" />
    </Icon>
  );
}

export function SparkleIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 3.2 13.7 8 18.5 9.7 13.7 11.4 12 16.2 10.3 11.4 5.5 9.7 10.3 8 12 3.2Z" />
      <path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2Z" />
    </Icon>
  );
}

export function LayersIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m12 2 9 5-9 5-9-5 9-5Z" />
      <path d="m3 12 9 5 9-5" />
      <path d="m3 17 9 5 9-5" />
    </Icon>
  );
}

export function ActivityIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M22 12h-3.5l-2.5 7-4-14-2.5 7H2" />
    </Icon>
  );
}

export function CommandIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M15 6a3 3 0 1 1 3 3h-3V6Z" />
      <path d="M15 18a3 3 0 1 0 3-3h-3v3Z" />
      <path d="M9 6a3 3 0 1 0-3 3h3V6Z" />
      <path d="M9 18a3 3 0 1 1-3-3h3v3Z" />
      <rect x="9" y="9" width="6" height="6" rx="0.5" />
    </Icon>
  );
}

export function ShieldIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 22s8-4 8-10V5.5L12 2 4 5.5V12c0 6 8 10 8 10Z" />
      <path d="m9 12 2 2 4-4" />
    </Icon>
  );
}

export function CpuIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="4.5" y="4.5" width="15" height="15" rx="2.5" />
      <rect x="9" y="9" width="6" height="6" rx="1" />
      <path d="M9 2v2.5M15 2v2.5M9 19.5V22M15 19.5V22M2 9h2.5M2 15h2.5M19.5 9H22M19.5 15H22" />
    </Icon>
  );
}

export function FileTextIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14 2.5H7a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7.5l-5-5Z" />
      <path d="M14 2.5v5h5" />
      <path d="M9 13h6M9 17h6M9 9h2" />
    </Icon>
  );
}

export function MailIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="2.5" y="5" width="19" height="14" rx="2.5" />
      <path d="m3.5 7 8.5 6 8.5-6" />
    </Icon>
  );
}

export function GlobeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="9.5" />
      <path d="M2.5 12h19" />
      <path d="M12 2.5c2.5 2.6 3.8 6 3.8 9.5s-1.3 6.9-3.8 9.5c-2.5-2.6-3.8-6-3.8-9.5S9.5 5.1 12 2.5Z" />
    </Icon>
  );
}

export function ChatIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M21 11.5a8.4 8.4 0 0 1-8.5 8.3 8.8 8.8 0 0 1-3.6-.75L3 21l1.9-5.2a8.1 8.1 0 0 1-.9-3.8A8.4 8.4 0 0 1 12.5 3 8.4 8.4 0 0 1 21 11.5Z" />
      <path d="M8.5 11.5h.01M12 11.5h.01M15.5 11.5h.01" />
    </Icon>
  );
}

export function CodeIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 5l-4 14" />
    </Icon>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M18 6 6 18M6 6l12 12" />
    </Icon>
  );
}

export function ChevronDownIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m6 9 6 6 6-6" />
    </Icon>
  );
}

export function ChevronRightIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m9 6 6 6-6 6" />
    </Icon>
  );
}

export function PauseIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="7" y="4.5" width="3.5" height="15" rx="1" />
      <rect x="13.5" y="4.5" width="3.5" height="15" rx="1" />
    </Icon>
  );
}

export function PlayIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M8 5.2v13.6a1 1 0 0 0 1.53.85l10.2-6.8a1 1 0 0 0 0-1.7L9.53 4.35A1 1 0 0 0 8 5.2Z" />
    </Icon>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M20.5 11a8.5 8.5 0 1 0-2.6 6.4" />
      <path d="M21 4.5V11h-6.5" />
    </Icon>
  );
}

export function TrashIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3.5 6.5h17M9 6.5v-2a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
      <path d="M18.5 6.5 17.6 20a1.5 1.5 0 0 1-1.5 1.4H7.9A1.5 1.5 0 0 1 6.4 20L5.5 6.5" />
      <path d="M10 11v6M14 11v6" />
    </Icon>
  );
}

export function FolderIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M3 7.5A2 2 0 0 1 5 5.5h3.6l2 2.2H19a2 2 0 0 1 2 2v8.3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7.5Z" />
    </Icon>
  );
}

export function PlusIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M12 5v14M5 12h14" />
    </Icon>
  );
}

export function CheckIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m20 6.5-11 11-5-5" />
    </Icon>
  );
}

export function AlertIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4.5M12 17.2h.01" />
    </Icon>
  );
}

export function ArrowUpIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m6 15 6-6 6 6" />
    </Icon>
  );
}

export function ArrowDownIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="m6 9 6 6 6-6" />
    </Icon>
  );
}

export function EnterIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M9 10 4 15l5 5" />
      <path d="M20 4v7a4 4 0 0 1-4 4H4" />
    </Icon>
  );
}

export function LockIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="4" y="10.5" width="16" height="10.5" rx="2.5" />
      <path d="M7.5 10.5V7a4.5 4.5 0 0 1 9 0v3.5" />
    </Icon>
  );
}

export function ExternalIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M14 4h6v6M20 4l-9 9" />
      <path d="M18 14v4.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4.5" />
    </Icon>
  );
}

export function CopyIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <rect x="9" y="9" width="12" height="12" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </Icon>
  );
}

export function ClockIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <circle cx="12" cy="12" r="9.5" />
      <path d="M12 6.5V12l3.5 2" />
    </Icon>
  );
}

export function DatabaseIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <ellipse cx="12" cy="5.5" rx="8.5" ry="3" />
      <path d="M20.5 12c0 1.66-3.8 3-8.5 3s-8.5-1.34-8.5-3" />
      <path d="M3.5 5.5v13c0 1.66 3.8 3 8.5 3s8.5-1.34 8.5-3v-13" />
    </Icon>
  );
}

export function HardDriveIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M21 12H3M5.4 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.4-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.8 1.1Z" />
      <path d="M6 16h.01M10 16h.01" />
    </Icon>
  );
}

export function SearchOffIcon(props: IconProps) {
  return (
    <Icon {...props}>
      <path d="M16.7 16.7 21 21" />
      <path d="M4.5 4.5 18 18" />
      <path d="M11 4a7 7 0 0 1 6.9 8.2M9.8 18.4A7 7 0 0 1 4 11.2" />
    </Icon>
  );
}
