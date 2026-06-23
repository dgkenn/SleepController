/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: 'class',
  content: [
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './lib/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        // token names kept stable; values tuned for a premium glass/gradient look
        brand: { DEFAULT: '#6c8cff', dark: '#5b9dff' },
        surface: {
          DEFAULT: '#070910',
          raised: 'rgba(255,255,255,0.06)',
          card: 'rgba(255,255,255,0.05)',
          border: 'rgba(255,255,255,0.10)',
        },
        cool: '#60a5fa',
        warm: '#fb923c',
        success: '#34d399',
        warning: '#fbbf24',
        danger: '#fb7185',
      },
      minHeight: { touch: '48px' },
      minWidth: { touch: '48px' },
      fontFamily: {
        sans: ['ui-rounded', 'SF Pro Rounded', 'Inter', 'system-ui', '-apple-system', 'sans-serif'],
      },
      borderRadius: { xl: '1rem', '2xl': '1.4rem', '3xl': '2rem' },
      boxShadow: {
        glow: '0 0 28px rgba(108,140,255,0.35)',
        soft: '0 10px 34px rgba(0,0,0,0.5)',
        inset: 'inset 0 1px 0 rgba(255,255,255,0.07)',
      },
      keyframes: {
        pulseDot: { '0%,100%': { opacity: 1 }, '50%': { opacity: 0.3 } },
        rise: { '0%': { opacity: 0, transform: 'translateY(10px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
      },
      animation: { pulseDot: 'pulseDot 2s ease-in-out infinite', rise: 'rise .4s cubic-bezier(.2,.7,.3,1) both' },
    },
  },
  plugins: [],
};
