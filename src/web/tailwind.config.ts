/**
 * TailwindCSS 4.x Theme Configuration
 * Synthetic ERP Data Generation Platform - Web Console
 *
 * Establishes the design system foundation for all 10 Web Console screens
 * (S-001 through S-010) with enterprise-appropriate color palettes,
 * typography, spacing, and component-level design tokens.
 *
 * Color System:
 *   - primary:   Enterprise blue for primary actions, active states, links
 *   - secondary: Gray-blue for secondary elements, navigation, headers
 *   - accent:    Indigo for highlights, badges, selected items
 *   - success:   Green for quality pass, successful operations, health OK
 *   - warning:   Amber for quality warnings, medium-severity alerts
 *   - error:     Red for failures, PII detection alerts, critical errors
 *   - neutral:   Gray scale for backgrounds, borders, disabled states
 *
 * Typography:
 *   - sans: Inter (body text, headings, UI labels)
 *   - mono: JetBrains Mono (code, data values, schema definitions)
 *
 * Plugins:
 *   - @tailwindcss/forms: Consistent form element styling across wizard steps
 *   - @tailwindcss/typography: Prose rendering for reports and documentation
 */
import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './index.html',
    './src/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      /**
       * Enterprise color palette with full shade ranges (50-950).
       * Each color is designed for WCAG 2.1 AA contrast compliance
       * when used with the recommended foreground/background pairings.
       */
      colors: {
        /** Enterprise blue — primary actions, links, focus rings, active navigation */
        primary: {
          50: '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
          950: '#172554',
        },
        /** Gray-blue (slate) — secondary elements, navigation sidebar, sub-headers */
        secondary: {
          50: '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
          950: '#020617',
        },
        /** Indigo — accent highlights, badges, selected items, data visualization */
        accent: {
          50: '#eef2ff',
          100: '#e0e7ff',
          200: '#c7d2fe',
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
          700: '#4338ca',
          800: '#3730a3',
          900: '#312e81',
          950: '#1e1b4b',
        },
        /** Green — quality pass (≥95%), successful generation, health OK, compliance certified */
        success: {
          50: '#f0fdf4',
          100: '#dcfce7',
          200: '#bbf7d0',
          300: '#86efac',
          400: '#4ade80',
          500: '#22c55e',
          600: '#16a34a',
          700: '#15803d',
          800: '#166534',
          900: '#14532d',
          950: '#052e16',
        },
        /** Amber — medium quality scores, job warnings, rate limit approaching */
        warning: {
          50: '#fffbeb',
          100: '#fef3c7',
          200: '#fde68a',
          300: '#fcd34d',
          400: '#fbbf24',
          500: '#f59e0b',
          600: '#d97706',
          700: '#b45309',
          800: '#92400e',
          900: '#78350f',
          950: '#451a03',
        },
        /** Red — errors, failed jobs, PII detection alerts, low quality scores */
        error: {
          50: '#fef2f2',
          100: '#fee2e2',
          200: '#fecaca',
          300: '#fca5a5',
          400: '#f87171',
          500: '#ef4444',
          600: '#dc2626',
          700: '#b91c1c',
          800: '#991b1b',
          900: '#7f1d1d',
          950: '#450a0a',
        },
        /** Gray — backgrounds, borders, disabled states, dividers */
        neutral: {
          50: '#fafafa',
          100: '#f5f5f5',
          200: '#e5e5e5',
          300: '#d4d4d4',
          400: '#a3a3a3',
          500: '#737373',
          600: '#525252',
          700: '#404040',
          800: '#262626',
          900: '#171717',
          950: '#0a0a0a',
        },
      },
      /**
       * Font families for the platform UI and data display.
       * Inter provides excellent readability for dashboard metrics and form labels.
       * JetBrains Mono is optimized for code, SQL, and schema definition display.
       */
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', 'monospace'],
      },
      /**
       * Border radius tokens for consistent component rounding.
       * Used across cards, buttons, inputs, modals, and badges.
       */
      borderRadius: {
        sm: '0.25rem',
        md: '0.375rem',
        lg: '0.5rem',
        xl: '0.75rem',
        '2xl': '1rem',
      },
      /**
       * Box shadow tokens for elevation hierarchy.
       * 'card' — subtle elevation for dashboard cards, list items, data tables
       * 'modal' — prominent elevation for modal dialogs, dropdowns, popovers
       */
      boxShadow: {
        card: '0 1px 3px rgba(0, 0, 0, 0.12), 0 1px 2px rgba(0, 0, 0, 0.06)',
        modal: '0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04)',
      },
      /**
       * Additional spacing tokens for layout widths and custom gaps.
       * '18'  (4.5rem)  — sidebar icon column width
       * '88'  (22rem)   — sidebar expanded width, medium panel width
       * '128' (32rem)   — wide panel width, modal default width
       */
      spacing: {
        '18': '4.5rem',
        '88': '22rem',
        '128': '32rem',
      },
      /**
       * Custom animations for loading and progress states.
       * 'spin-slow'  — gentle rotation for background loading indicators
       * 'pulse-slow' — subtle pulsing for skeleton loaders and heartbeat indicators
       */
      animation: {
        'spin-slow': 'spin 3s linear infinite',
        'pulse-slow': 'pulse 3s ease-in-out infinite',
      },
    },
  },
  /**
   * TailwindCSS plugins:
   * - @tailwindcss/forms: Opinionated form resets for consistent input, select,
   *   textarea, and checkbox styling across the Generation Wizard (S-002),
   *   Admin Panel (S-009), and Settings (S-010) screens.
   * - @tailwindcss/typography: Prose utility classes for rendered markdown
   *   in Quality Reports (S-007), Compliance Dashboard (S-008), and
   *   schema descriptions in the Schema Browser (S-005).
   */
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography'),
  ],
};

export default config satisfies Config;
