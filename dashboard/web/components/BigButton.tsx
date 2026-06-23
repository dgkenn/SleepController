'use client';

import { ButtonHTMLAttributes } from 'react';

interface BigButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  loading?: boolean;
  fullWidth?: boolean;
}

const variants = {
  primary: 'bg-brand hover:bg-brand-dark active:bg-brand-dark text-white',
  secondary: 'bg-surface-raised hover:bg-surface-border active:bg-surface-border text-white border border-surface-border',
  danger: 'bg-danger hover:bg-red-700 active:bg-red-800 text-white',
  ghost: 'bg-transparent hover:bg-surface-raised active:bg-surface-card text-gray-300 border border-surface-border',
};

export default function BigButton({
  variant = 'primary',
  loading = false,
  fullWidth = false,
  children,
  disabled,
  className = '',
  ...props
}: BigButtonProps) {
  return (
    <button
      {...props}
      disabled={disabled || loading}
      className={`
        inline-flex items-center justify-center gap-2
        min-h-[52px] px-6 rounded-xl font-semibold text-base
        transition-all duration-150
        disabled:opacity-40 disabled:cursor-not-allowed
        active:scale-[0.97]
        ${variants[variant]}
        ${fullWidth ? 'w-full' : ''}
        ${className}
      `}
    >
      {loading && (
        <svg className="w-5 h-5 animate-spin" viewBox="0 0 24 24" fill="none">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
          />
        </svg>
      )}
      {children}
    </button>
  );
}
