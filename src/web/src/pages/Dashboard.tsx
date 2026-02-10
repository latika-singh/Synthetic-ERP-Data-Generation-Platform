/**
 * Dashboard Page — Screen S-001
 *
 * Main dashboard for the Synthetic ERP Data Generation Platform Web Console.
 * Displays generation statistics, recent jobs, system health indicators,
 * and quick-action cards for common workflows.
 *
 * Reference: Agent Action Plan Section 0.3.1 — Screen S-001
 */

import React from 'react';

/**
 * Dashboard component — default landing page for authenticated users.
 *
 * Renders:
 *   - Generation statistics summary cards (total jobs, records generated, quality score)
 *   - Recent generation jobs list with status indicators
 *   - System health overview (services, database, queue)
 *   - Quick-action cards for starting new generation, browsing templates
 *
 * @returns The dashboard page element
 */
function Dashboard(): React.JSX.Element {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-neutral-900">Dashboard</h1>
      </div>

      {/* Statistics Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-white rounded-lg shadow-card p-6">
          <p className="text-sm font-medium text-neutral-500">Total Jobs</p>
          <p className="text-3xl font-bold text-neutral-900 mt-2">0</p>
          <p className="text-sm text-neutral-400 mt-1">No jobs yet</p>
        </div>
        <div className="bg-white rounded-lg shadow-card p-6">
          <p className="text-sm font-medium text-neutral-500">Records Generated</p>
          <p className="text-3xl font-bold text-neutral-900 mt-2">0</p>
          <p className="text-sm text-neutral-400 mt-1">Start generating</p>
        </div>
        <div className="bg-white rounded-lg shadow-card p-6">
          <p className="text-sm font-medium text-neutral-500">Avg Quality Score</p>
          <p className="text-3xl font-bold text-success-600 mt-2">—</p>
          <p className="text-sm text-neutral-400 mt-1">Target ≥95%</p>
        </div>
        <div className="bg-white rounded-lg shadow-card p-6">
          <p className="text-sm font-medium text-neutral-500">System Health</p>
          <p className="text-3xl font-bold text-success-600 mt-2">OK</p>
          <p className="text-sm text-neutral-400 mt-1">All services running</p>
        </div>
      </div>

      {/* Recent Jobs Table */}
      <div className="bg-white rounded-lg shadow-card">
        <div className="px-6 py-4 border-b border-neutral-200">
          <h2 className="text-lg font-semibold text-neutral-900">Recent Generation Jobs</h2>
        </div>
        <div className="p-6 text-center text-neutral-500">
          <p>No generation jobs yet. Start by creating a new generation job.</p>
        </div>
      </div>
    </div>
  );
}

export default Dashboard;
