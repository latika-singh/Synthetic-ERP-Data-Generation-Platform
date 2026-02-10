/**
 * React Application Entry Point
 *
 * Bootstraps the Synthetic ERP Data Generation Platform Web Console by:
 *   1. Locating the #root DOM element from index.html
 *   2. Creating a React 19.x concurrent mode root via ReactDOM.createRoot()
 *   3. Rendering the App component (which provides Auth0, Router, and Error Boundary)
 *   4. Importing the global TailwindCSS stylesheet for design system injection
 *
 * This file is referenced by index.html as:
 *   <script type="module" src="/src/main.tsx"></script>
 *
 * Vite handles module resolution, HMR during development, and chunk
 * optimization during production builds from this entry point.
 *
 * Reference: Agent Action Plan Section 0.3.1 — src/web/src/main.tsx
 */

import ReactDOM from 'react-dom/client';
import App from './App';
import './index.css';

/**
 * Locate the root DOM element where React will mount the application.
 * The element must exist in index.html as: <div id="root"></div>
 *
 * This assertion provides a clear error message during development if
 * the root element is missing, rather than a cryptic null reference error.
 */
const rootElement = document.getElementById('root');

if (!rootElement) {
  throw new Error(
    'Failed to find the root element. ' +
      'Ensure index.html contains <div id="root"></div>. ' +
      'The Synthetic ERP Data Generation Platform Web Console requires ' +
      'this element as the React mounting point.',
  );
}

/**
 * Create a React 19.x concurrent mode root and render the application.
 *
 * ReactDOM.createRoot() enables:
 *   - Concurrent rendering for responsive UI during heavy computation
 *   - Automatic batching of state updates for performance
 *   - Transitions API for non-urgent UI updates
 *
 * The App component provides the complete provider stack:
 *   React.StrictMode → ErrorBoundary → Auth0Provider → Suspense → RouterProvider
 */
const root = ReactDOM.createRoot(rootElement);
root.render(<App />);
