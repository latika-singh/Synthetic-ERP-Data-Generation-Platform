/**
 * Vite 5.x Build Configuration
 *
 * Configures the Vite build tool for the Synthetic ERP Data Generation Platform
 * Web Console. This file defines:
 *   - React 19.x plugin for JSX transform and Fast Refresh (HMR)
 *   - Path aliases mirroring tsconfig.json paths (@/ -> src/)
 *   - Development server proxy to API Gateway (Flask backend)
 *   - Production build optimization with vendor chunk splitting
 *   - Vitest test configuration with jsdom environment
 *
 * Reference: Agent Action Plan Section 0.3.1 — src/web/vite.config.ts
 */

/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  /**
   * Plugins configuration.
   * react() enables:
   *   - Automatic JSX transform (React 19 — no manual React import needed)
   *   - Fast Refresh for instantaneous HMR during development
   *   - Babel integration for JSX/TSX processing in the build pipeline
   */
  plugins: [react()],

  /**
   * Module resolution configuration.
   * Mirrors the tsconfig.json path aliases so that Vite resolves
   * '@/components/...' to 'src/components/...' at both dev and build time.
   */
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },

  /**
   * Development server configuration.
   * - port 3000: Standard frontend dev port, matches docker-compose.yml mapping
   * - host true: Binds to 0.0.0.0 for Docker container accessibility
   * - proxy: Forwards /api requests to the Flask API Gateway on port 5000,
   *   enabling local development without CORS configuration
   */
  server: {
    port: 3000,
    host: true,
    proxy: {
      '/api': {
        target: 'http://localhost:5000',
        changeOrigin: true,
        secure: false,
      },
    },
    fs: {
      allow: ['../..'],
    },
  },

  /**
   * Production build configuration.
   * - outDir: Outputs to 'dist/' for nginx serving in Dockerfile Stage 2
   * - sourcemap: Disabled in production for security; enabled in dev via Vite default
   * - target: ES2020 matching tsconfig.json target for modern browser support
   * - manualChunks: Splits vendor libraries into separate chunks for:
   *     - Improved caching (vendor changes less frequently than app code)
   *     - Parallel loading of independent library bundles
   *     - Reduced main bundle size for faster initial page load
   * - chunkSizeWarningLimit: Raised to 1000 KB for large vendor bundles (recharts)
   */
  build: {
    outDir: 'dist',
    sourcemap: false,
    target: 'es2020',
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-router-dom'],
          'vendor-state': ['zustand'],
          'vendor-charts': ['recharts'],
          'vendor-auth': ['@auth0/auth0-react'],
          'vendor-http': ['axios'],
        },
      },
    },
  },

  /**
   * Vitest test configuration.
   * Integrated into vite.config.ts to share path aliases and plugin configuration.
   * - globals: true enables describe/it/expect without explicit imports
   * - environment: 'jsdom' provides browser-like DOM for component testing
   * - setupFiles: Points to test setup files for @testing-library/jest-dom matchers
   *   and comprehensive browser API mocks (matchMedia, crypto, IntersectionObserver, etc.)
   * - include: Covers tests inside src/ and external tests in tests/unit/web/
   * - css: true processes CSS imports during tests (TailwindCSS)
   */
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: [
      './src/test/setup.ts',
      '../../tests/unit/web/setup.ts',
    ],
    include: [
      'src/**/*.{test,spec}.{ts,tsx}',
      '../../tests/unit/web/**/*.{test,spec}.{ts,tsx}',
    ],
    css: true,
    alias: {
      '@testing-library/jest-dom': path.resolve(__dirname, 'node_modules/@testing-library/jest-dom'),
      '@testing-library/react': path.resolve(__dirname, 'node_modules/@testing-library/react'),
      /**
       * Recharts alias ensures that vi.mock('recharts') in external test files
       * (tests/unit/web/) resolves to the same physical module as the source
       * component imports. Without this, vitest resolves the mock path relative
       * to the test file location (outside src/web/), which fails to match the
       * component's import resolution path (src/web/node_modules/recharts).
       */
      'recharts': path.resolve(__dirname, 'node_modules/recharts'),
    },
  },
});
