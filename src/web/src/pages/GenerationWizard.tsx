/**
 * Generation Wizard Page — Screen S-002
 *
 * Multi-step wizard for creating new synthetic data generation jobs.
 * Steps: Method Selection → Schema Configuration → Parameters → Review & Submit
 *
 * Reference: Agent Action Plan Section 0.3.1 — Screen S-002
 */

import React, { useState } from 'react';

/**
 * Wizard step identifier type.
 */
type WizardStep = 'method' | 'schema' | 'parameters' | 'review';

/**
 * Step metadata for rendering the step indicator.
 */
interface StepInfo {
  id: WizardStep;
  label: string;
  description: string;
}

/** Wizard step definitions */
const steps: StepInfo[] = [
  { id: 'method', label: 'Method', description: 'Select generation method' },
  { id: 'schema', label: 'Schema', description: 'Configure schema and tables' },
  { id: 'parameters', label: 'Parameters', description: 'Set generation parameters' },
  { id: 'review', label: 'Review', description: 'Review and submit' },
];

/**
 * GenerationWizard component — multi-step generation job creation.
 *
 * @returns The generation wizard page element
 */
function GenerationWizard(): React.JSX.Element {
  const [currentStep, setCurrentStep] = useState<WizardStep>('method');
  const currentIndex = steps.findIndex((s) => s.id === currentStep);

  const handleNext = (): void => {
    if (currentIndex < steps.length - 1) {
      setCurrentStep(steps[currentIndex + 1].id);
    }
  };

  const handleBack = (): void => {
    if (currentIndex > 0) {
      setCurrentStep(steps[currentIndex - 1].id);
    }
  };

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold text-neutral-900">New Generation Job</h1>

      {/* Step Indicator */}
      <nav aria-label="Wizard progress" className="flex items-center space-x-4">
        {steps.map((step, index) => (
          <div key={step.id} className="flex items-center">
            <div
              className={`flex items-center justify-center w-8 h-8 rounded-full text-sm font-semibold ${
                index <= currentIndex
                  ? 'bg-primary-600 text-white'
                  : 'bg-neutral-200 text-neutral-500'
              }`}
            >
              {index + 1}
            </div>
            <span
              className={`ml-2 text-sm font-medium ${
                index <= currentIndex ? 'text-primary-600' : 'text-neutral-400'
              }`}
            >
              {step.label}
            </span>
            {index < steps.length - 1 && (
              <div className="w-12 h-0.5 mx-2 bg-neutral-200" />
            )}
          </div>
        ))}
      </nav>

      {/* Step Content */}
      <div className="bg-white rounded-lg shadow-card p-6">
        <h2 className="text-lg font-semibold text-neutral-900 mb-2">
          {steps[currentIndex].label}
        </h2>
        <p className="text-neutral-500 mb-6">{steps[currentIndex].description}</p>

        {currentStep === 'method' && (
          <div className="grid grid-cols-2 gap-4">
            {['AI/ML Generation', 'Rules-Based', 'Statistical Synthesis', 'Intelligent Masking'].map(
              (method) => (
                <button
                  key={method}
                  type="button"
                  className="p-4 border border-neutral-200 rounded-lg text-left hover:border-primary-500 hover:bg-primary-50 transition-colors"
                >
                  <p className="font-medium text-neutral-900">{method}</p>
                </button>
              ),
            )}
          </div>
        )}

        {currentStep === 'schema' && (
          <p className="text-neutral-500">Select ERP schema and tables for generation.</p>
        )}

        {currentStep === 'parameters' && (
          <p className="text-neutral-500">Configure record count, batch size, and output format.</p>
        )}

        {currentStep === 'review' && (
          <p className="text-neutral-500">Review configuration and submit generation job.</p>
        )}
      </div>

      {/* Navigation Buttons */}
      <div className="flex justify-between">
        <button
          type="button"
          onClick={handleBack}
          disabled={currentIndex === 0}
          className="px-4 py-2 text-neutral-600 border border-neutral-300 rounded-md hover:bg-neutral-50 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Back
        </button>
        <button
          type="button"
          onClick={handleNext}
          className="px-4 py-2 bg-primary-600 text-white rounded-md hover:bg-primary-700"
        >
          {currentIndex === steps.length - 1 ? 'Submit' : 'Next'}
        </button>
      </div>
    </div>
  );
}

export default GenerationWizard;
