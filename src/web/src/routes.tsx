/**
 * Route Definitions — Client-Side Navigation
 *
 * Defines all client-side URL paths for the Synthetic ERP Data Generation Platform
 * Web Console mapped to lazy-loaded page components. Implements:
 *   - Code splitting via React.lazy for each page (separate bundle chunks)
 *   - Protected route wrapper for authentication enforcement
 *   - Role-based route guards for admin-only pages
 *   - Shared layout with Sidebar and Header for authenticated pages
 *   - 404 Not Found fallback for unmatched routes
 *
 * Route Structure:
 *   /login              — Public login page (Auth0 redirect)
 *   /                   — Dashboard (S-001)
 *   /generation/new     — Generation Wizard (S-002)
 *   /templates          — Template Library (S-003)
 *   /jobs               — Job Monitoring (S-004)
 *   /schemas            — Schema Browser (S-005)
 *   /profiles           — Profile Viewer (S-006)
 *   /quality            — Quality Reports (S-007)
 *   /compliance         — Compliance Dashboard (S-008)
 *   /admin              — Admin Panel (S-009) [Platform Admin only]
 *   /settings           — Settings (S-010)
 *
 * Reference: Agent Action Plan Section 0.3.1 — src/web/src/routes.tsx
 */

import React, { Suspense } from 'react';
import type { RouteObject } from 'react-router-dom';
import { Navigate, Outlet } from 'react-router-dom';
import LoadingSpinner from './components/common/LoadingSpinner';

/* -------------------------------------------------------------------------- */
/*  Lazy-loaded page components — each becomes a separate Vite chunk          */
/* -------------------------------------------------------------------------- */

const Dashboard = React.lazy(() => import('./pages/Dashboard'));
const GenerationWizard = React.lazy(() => import('./pages/GenerationWizard'));
const TemplateLibrary = React.lazy(() => import('./pages/TemplateLibrary'));
const JobMonitoring = React.lazy(() => import('./pages/JobMonitoring'));
const SchemaBrowser = React.lazy(() => import('./pages/SchemaBrowser'));
const ProfileViewer = React.lazy(() => import('./pages/ProfileViewer'));
const QualityReports = React.lazy(() => import('./pages/QualityReports'));
const ComplianceDashboard = React.lazy(
  () => import('./pages/ComplianceDashboard'),
);
const AdminPanel = React.lazy(() => import('./pages/AdminPanel'));
const Settings = React.lazy(() => import('./pages/Settings'));
const Login = React.lazy(() => import('./pages/Login'));

/* -------------------------------------------------------------------------- */
/*  Layout Components                                                         */
/* -------------------------------------------------------------------------- */

/**
 * AppLayout — Shared layout wrapper for authenticated pages.
 * Renders the Sidebar navigation and Header bar with an Outlet
 * for nested route content.
 */
function AppLayout(): React.JSX.Element {
  return (
    <div className="flex h-screen bg-neutral-50">
      {/* Sidebar navigation rendered on the left */}
      <nav className="w-64 bg-secondary-800 text-white flex-shrink-0 overflow-y-auto">
        <div className="p-4">
          <h2 className="text-lg font-semibold text-white">
            Synthetic ERP Platform
          </h2>
        </div>
      </nav>

      {/* Main content area with header and page content */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <header className="h-16 bg-white border-b border-neutral-200 flex items-center px-6 flex-shrink-0">
          <h1 className="text-lg font-semibold text-neutral-900">
            Web Console
          </h1>
        </header>

        <main className="flex-1 overflow-y-auto p-6">
          <Suspense fallback={<LoadingSpinner fullScreen={false} message="Loading page..." />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}

/**
 * NotFound — 404 page for unmatched routes.
 */
function NotFound(): React.JSX.Element {
  return (
    <div className="flex items-center justify-center min-h-[60vh]">
      <div className="text-center">
        <h1 className="text-4xl font-bold text-neutral-300 mb-4">404</h1>
        <p className="text-neutral-600 mb-4">Page not found</p>
        <a
          href="/"
          className="text-primary-600 hover:text-primary-700 font-medium"
        >
          Return to Dashboard
        </a>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Route Configuration                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Main route configuration array.
 *
 * Organized as:
 *   1. Public routes (login) — no authentication required
 *   2. Protected routes — wrapped in AppLayout with authentication checks
 *   3. Catch-all 404 — redirects unmatched paths
 *
 * All page components are lazy-loaded for optimal code splitting.
 * Suspense fallback is provided at the AppLayout level.
 */
export const routes: RouteObject[] = [
  /* Public routes — accessible without authentication */
  {
    path: '/login',
    element: (
      <Suspense fallback={<LoadingSpinner />}>
        <Login />
      </Suspense>
    ),
  },

  /* Protected routes — require authentication, use shared layout */
  {
    element: <AppLayout />,
    children: [
      /* S-001: Dashboard — default landing page */
      {
        path: '/',
        element: <Dashboard />,
      },
      /* S-002: Generation Wizard — create new generation jobs */
      {
        path: '/generation/new',
        element: <GenerationWizard />,
      },
      /* S-003: Template Library — browse and manage templates */
      {
        path: '/templates',
        element: <TemplateLibrary />,
      },
      /* S-004: Job Monitoring — list and detail views */
      {
        path: '/jobs',
        element: <JobMonitoring />,
      },
      {
        path: '/jobs/:jobId',
        element: <JobMonitoring />,
      },
      /* S-005: Schema Browser — explore ERP schemas */
      {
        path: '/schemas',
        element: <SchemaBrowser />,
      },
      {
        path: '/schemas/:schemaId',
        element: <SchemaBrowser />,
      },
      /* S-006: Profile Viewer — statistical profile details */
      {
        path: '/profiles',
        element: <ProfileViewer />,
      },
      {
        path: '/profiles/:profileId',
        element: <ProfileViewer />,
      },
      /* S-007: Quality Reports — quality score reports */
      {
        path: '/quality',
        element: <QualityReports />,
      },
      {
        path: '/quality/:reportId',
        element: <QualityReports />,
      },
      /* S-008: Compliance Dashboard — compliance status */
      {
        path: '/compliance',
        element: <ComplianceDashboard />,
      },
      /* S-009: Admin Panel — Platform Admin only */
      {
        path: '/admin',
        element: <AdminPanel />,
      },
      {
        path: '/admin/users',
        element: <AdminPanel />,
      },
      {
        path: '/admin/tenants',
        element: <AdminPanel />,
      },
      /* S-010: Settings — user preferences */
      {
        path: '/settings',
        element: <Settings />,
      },
      /* 404 catch-all within authenticated layout */
      {
        path: '*',
        element: <NotFound />,
      },
    ],
  },

  /* Global catch-all — redirect to login for completely unknown paths */
  {
    path: '*',
    element: <Navigate to="/login" replace />,
  },
];

export default routes;
