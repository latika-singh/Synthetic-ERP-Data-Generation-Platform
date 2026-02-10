/**
 * AdminPanel Page Component
 *
 * Page component for the Synthetic ERP Data Generation Platform Web Console.
 * Reference: Agent Action Plan Section 0.3.1
 */

import React from 'react';

/**
 * AdminPanel component.
 * @returns The AdminPanel page element
 */
function AdminPanel(): React.JSX.Element {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-neutral-900">AdminPanel</h1>
      <div className="bg-white rounded-lg shadow-card p-6">
        <p className="text-neutral-500">AdminPanel content area.</p>
      </div>
    </div>
  );
}

export default AdminPanel;
