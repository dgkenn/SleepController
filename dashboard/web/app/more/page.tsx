'use client';

import Link from 'next/link';
import AuthGuard from '@/components/AuthGuard';
import BottomNav from '@/components/BottomNav';

interface MoreLink {
  href: string;
  title: string;
  description: string;
  icon: React.ReactNode;
}

const LINKS: MoreLink[] = [
  {
    href: '/diagnostics',
    title: 'Diagnostics',
    description: 'Fused health verdict, checks, and recent system events',
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" className="w-6 h-6">
        <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
      </svg>
    ),
  },
  {
    href: '/admin',
    title: 'Admin',
    description: 'Daemon health, data sources, phone sensor, bed self-test',
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" className="w-6 h-6">
        <path d="M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm0 10.99h7c-.53 4.12-3.28 7.79-7 8.94V12H5V6.3l7-3.11v8.8z" />
      </svg>
    ),
  },
  {
    href: '/settings',
    title: 'Settings',
    description: 'Tune neutral temp, wake ramp, and thresholds',
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" className="w-6 h-6">
        <path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.488.488 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.44.17-.48.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87a.49.49 0 00.12.61l2.03 1.58c-.05.3-.09.63-.09.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.25.41.48.41h3.84c.24 0 .44-.17.48-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32a.49.49 0 00-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z" />
      </svg>
    ),
  },
];

function MoreContent() {
  return (
    <div className="flex flex-col min-h-screen">
      <div className="flex-1 overflow-y-auto pb-24">
        <div className="px-4 pt-14 pb-4">
          <h1 className="text-xl font-bold text-white mb-1">More</h1>
          <p className="text-sm text-gray-500">System health, admin tools, and settings</p>
        </div>

        <div className="px-4 space-y-3">
          {LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="flex items-center gap-3 bg-surface-card rounded-2xl p-4 border border-surface-border active:bg-surface-raised transition-colors min-h-[44px]"
            >
              <span className="text-brand shrink-0">{link.icon}</span>
              <div className="min-w-0 flex-1">
                <p className="text-sm font-semibold text-white">{link.title}</p>
                <p className="text-xs text-gray-500">{link.description}</p>
              </div>
              <svg className="w-4 h-4 text-gray-600 shrink-0" viewBox="0 0 24 24" fill="currentColor">
                <path d="M8.59 16.59L13.17 12 8.59 7.41 10 6l6 6-6 6z" />
              </svg>
            </Link>
          ))}
        </div>
      </div>

      <BottomNav />
    </div>
  );
}

export default function MorePage() {
  return (
    <AuthGuard>
      <MoreContent />
    </AuthGuard>
  );
}
