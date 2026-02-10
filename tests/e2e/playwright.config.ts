/**
 * Playwright E2E Test Configuration
 *
 * Configures the Playwright test runner for the Synthetic ERP Data Generation
 * Platform Web Console. Defines browser targets, base URL, timeouts, screenshot
 * and video capture settings, test directory, and the web server auto-start
 * command pointing to the Vite development server.
 *
 * All E2E spec files (generation-wizard.spec.ts, dashboard.spec.ts,
 * authentication.spec.ts) depend on this configuration for execution.
 *
 * Reference: Agent Action Plan Section 0.3.1 — tests/e2e/playwright.config.ts
 */

import { defineConfig, devices } from '@playwright/test';

/**
 * Default exported Playwright configuration object.
 *
 * @see https://playwright.dev/docs/test-configuration
 */
export default defineConfig({
  /**
   * Directory containing the E2E test spec files.
   * Relative to this configuration file.
   */
  testDir: './tests',

  /**
   * Run tests in files in parallel for faster execution.
   * Each spec file's tests run sequentially within the file,
   * but different spec files execute concurrently.
   */
  fullyParallel: true,

  /**
   * Fail the build on CI if test.only() is accidentally left in source code.
   * Prevents focused tests from silently skipping other tests in CI pipelines.
   */
  forbidOnly: !!process.env.CI,

  /**
   * Retry failed tests in CI to handle transient failures (network, timing).
   * No retries locally for faster feedback during development.
   */
  retries: process.env.CI ? 2 : 0,

  /**
   * Limit parallel workers in CI for stability and predictable resource usage.
   * Locally, Playwright auto-detects optimal worker count from CPU cores.
   */
  workers: process.env.CI ? 1 : undefined,

  /**
   * Default timeout for each test in milliseconds (30 seconds).
   * Individual tests can override this with test.setTimeout().
   */
  timeout: 30_000,

  /**
   * Assertion-specific timeout configuration.
   */
  expect: {
    /**
     * Maximum time an expect() assertion will wait for the condition
     * to be met before failing (5 seconds).
     */
    timeout: 5_000,
  },

  /**
   * Reporter configuration.
   * - CI: HTML report (persisted as artifact) + JUnit XML for CI integration
   * - Local: List reporter for concise terminal output
   */
  reporter: process.env.CI
    ? [
        ['html', { open: 'never' }],
        ['junit', { outputFile: 'results/e2e-results.xml' }],
      ]
    : 'list',

  /**
   * Shared settings applied to all test projects (browser targets).
   * These can be overridden per-project in the `projects` array below.
   */
  use: {
    /**
     * Base URL for all page.goto('/...') navigations.
     * Defaults to the Vite dev server port (3000) matching vite.config.ts.
     * Override via the BASE_URL environment variable for staging/production testing.
     */
    baseURL: process.env.BASE_URL || 'http://localhost:3000',

    /**
     * Capture execution trace on the first retry of a failed test.
     * Traces include DOM snapshots, network requests, and console logs,
     * invaluable for debugging flaky failures in CI.
     */
    trace: 'on-first-retry',

    /**
     * Capture a screenshot only when a test fails.
     * Keeps test output clean while preserving failure evidence.
     */
    screenshot: 'only-on-failure',

    /**
     * Record video but retain only for failed tests.
     * Reduces storage while providing visual debugging for failures.
     */
    video: 'retain-on-failure',

    /**
     * Maximum time for individual user actions (click, fill, select).
     * 10 seconds accommodates slower CI environments and complex UI transitions.
     */
    actionTimeout: 10_000,

    /**
     * Maximum time for page navigations (goto, waitForURL).
     * 15 seconds accounts for Vite HMR compilation and API mock setup.
     */
    navigationTimeout: 15_000,
  },

  /**
   * Browser targets for cross-browser E2E testing.
   * Each project runs the full test suite against a specific browser engine.
   */
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
  ],

  /**
   * Web server configuration for automatic dev server lifecycle management.
   * Playwright starts the Vite dev server before tests and tears it down after.
   *
   * - command: Runs `npm run dev` in the Web Console directory
   * - url: Health-check URL to confirm the server is ready
   * - cwd: Working directory relative to this config (../../src/web)
   * - reuseExistingServer: In local dev, reuses an already-running server
   * - timeout: 120 seconds for initial cold start (npm install + Vite compile)
   */
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    cwd: '../../src/web',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },

  /**
   * Directory for test artifacts (screenshots, videos, traces).
   * Relative to this configuration file.
   */
  outputDir: './test-results',
});
