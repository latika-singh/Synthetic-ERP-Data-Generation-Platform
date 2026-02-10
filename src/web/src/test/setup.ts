/**
 * Vitest Test Setup File
 *
 * Configures the test environment for the Synthetic ERP Data Generation Platform
 * Web Console. This file is executed before each test suite and sets up:
 *   - @testing-library/jest-dom custom matchers (toBeInTheDocument, toHaveAttribute, etc.)
 *   - Global test environment configuration
 *
 * Referenced by vite.config.ts test.setupFiles configuration.
 */

import '@testing-library/jest-dom';
