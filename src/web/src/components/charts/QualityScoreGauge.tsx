/**
 * QualityScoreGauge.tsx
 *
 * Recharts-based React/TypeScript circular radial gauge component that renders
 * the weighted composite quality score with visual breakdowns for:
 *   - Statistical Fidelity (40% weight)
 *   - Business Rules Compliance (30% weight)
 *   - Referential Integrity (30% weight)
 *
 * Displays the overall score as a radial bar with color-coded thresholds
 * (green ≥95%, amber 80-95%, orange 60-80%, red <60%), individual sub-score
 * progress bars, and a numeric center label.
 *
 * Used by:
 *   - QualityReports page (Screen S-007) for detailed quality score display
 *   - Dashboard page (Screen S-001) for at-a-glance quality overview
 *
 * Consumes quality score data from generation job results.
 * Follows TailwindCSS 4.x styling conventions with dark mode support.
 */

import React, { useMemo } from 'react';
import {
  RadialBarChart,
  RadialBar,
  ResponsiveContainer,
  PolarAngleAxis,
  Cell,
  Legend,
  Tooltip,
} from 'recharts';

// ---------------------------------------------------------------------------
// Exported Interfaces
// ---------------------------------------------------------------------------

/**
 * Breakdown of individual quality dimension sub-scores.
 * Each sub-score is a normalized value between 0.0 and 1.0.
 */
export interface QualityScoreBreakdown {
  /** Statistical fidelity sub-score (0.0 - 1.0), contributes 40% to overall */
  statistical: number;
  /** Business rules compliance sub-score (0.0 - 1.0), contributes 30% to overall */
  businessRules: number;
  /** Referential integrity sub-score (0.0 - 1.0), contributes 30% to overall */
  referentialIntegrity: number;
}

/**
 * Props for the QualityScoreGauge component.
 */
export interface QualityScoreGaugeProps {
  /** Overall weighted quality score (0.0 - 1.0) */
  overallScore: number;
  /** Optional breakdown of individual quality dimensions */
  breakdown?: QualityScoreBreakdown;
  /** Size of the gauge: 'sm' (150px), 'md' (200px), 'lg' (280px) */
  size?: 'sm' | 'md' | 'lg';
  /** Title label displayed above the gauge */
  title?: string;
  /** Whether to show the score breakdown legend below */
  showBreakdown?: boolean;
  /** Quality threshold target line (default 0.95) */
  threshold?: number;
  /** Whether the component is in a loading state */
  isLoading?: boolean;
  /** Optional CSS class name for the container */
  className?: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Maps size prop values to pixel dimensions */
const SIZE_MAP: Record<string, number> = {
  sm: 150,
  md: 200,
  lg: 280,
};

/** Weighted contribution of each quality dimension to the composite score */
const SCORE_WEIGHTS = {
  statistical: 0.4,
  businessRules: 0.3,
  referentialIntegrity: 0.3,
} as const;

/**
 * Returns the appropriate color for a given quality score based on thresholds.
 * - Green (#10B981)  for scores ≥ 0.95 (meets target)
 * - Amber (#F59E0B)  for scores 0.80 – 0.94 (acceptable)
 * - Orange (#F97316)  for scores 0.60 – 0.79 (warning)
 * - Red (#EF4444)    for scores < 0.60 (critical)
 */
const getScoreColor = (score: number): string => {
  if (score >= 0.95) return '#10B981'; // green-500 (meets target)
  if (score >= 0.80) return '#F59E0B'; // amber-500 (acceptable)
  if (score >= 0.60) return '#F97316'; // orange-500 (warning)
  return '#EF4444'; // red-500 (critical)
};

/** Sub-score colors for breakdown visualization */
const BREAKDOWN_COLORS = {
  statistical: '#3B82F6',         // blue-500
  businessRules: '#8B5CF6',       // violet-500
  referentialIntegrity: '#06B6D4', // cyan-500
} as const;

/** Human-readable labels for each breakdown dimension */
const BREAKDOWN_LABELS: Record<string, string> = {
  statistical: 'Statistical Fidelity',
  businessRules: 'Business Rules',
  referentialIntegrity: 'Referential Integrity',
};

// ---------------------------------------------------------------------------
// Internal helper types for chart data
// ---------------------------------------------------------------------------

interface GaugeDataEntry {
  name: string;
  value: number;
  fill: string;
}

interface BreakdownDataEntry {
  name: string;
  value: number;
  fill: string;
  weight: string;
}

// ---------------------------------------------------------------------------
// Custom Tooltip Component
// ---------------------------------------------------------------------------

interface CustomTooltipProps {
  active?: boolean;
  payload?: Array<{ payload: GaugeDataEntry | BreakdownDataEntry }>;
}

/**
 * Custom tooltip rendered on hover over the radial bar chart.
 * Shows the metric name, score percentage, and styled color indicator.
 */
const CustomGaugeTooltip: React.FC<CustomTooltipProps> = ({ active, payload }): JSX.Element | null => {
  if (!active || !payload || payload.length === 0) {
    return null;
  }

  const data = payload[0].payload;

  return (
    <div className="bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded-lg shadow-lg px-3 py-2">
      <div className="flex items-center gap-2">
        <div
          className="w-2.5 h-2.5 rounded-full flex-shrink-0"
          style={{ backgroundColor: data.fill }}
        />
        <span className="text-xs font-medium text-gray-700 dark:text-gray-200">
          {data.name}
        </span>
      </div>
      <div className="text-sm font-semibold text-gray-900 dark:text-white mt-1">
        {data.value}%
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Loading Skeleton Component
// ---------------------------------------------------------------------------

interface LoadingSkeletonProps {
  chartSize: number;
  className: string;
  title?: string;
}

/**
 * Animated loading placeholder shown while quality score data is being
 * fetched. Matches the dimensions of the actual gauge for seamless transition.
 */
const LoadingSkeleton: React.FC<LoadingSkeletonProps> = ({
  chartSize,
  className,
  title,
}): JSX.Element => {
  return (
    <div
      className={`bg-white dark:bg-gray-800 rounded-lg shadow-sm p-4 ${className}`}
      role="status"
      aria-label="Loading quality score"
    >
      {title && (
        <div className="flex justify-center mb-2">
          <div className="h-4 w-24 bg-gray-200 dark:bg-gray-600 rounded animate-pulse" />
        </div>
      )}
      <div
        className="mx-auto flex items-center justify-center"
        style={{ width: chartSize, height: chartSize }}
      >
        <div
          className="rounded-full border-8 border-gray-200 dark:border-gray-600 animate-pulse"
          style={{
            width: chartSize * 0.75,
            height: chartSize * 0.75,
          }}
        />
      </div>
      <div className="flex justify-center mt-3">
        <span className="text-xs text-gray-400 dark:text-gray-500 animate-pulse">
          Loading quality data…
        </span>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

/**
 * QualityScoreGauge renders a circular radial bar chart visualizing the
 * weighted composite quality score for synthetic data generation results.
 *
 * The gauge uses a 270° arc (startAngle=225, endAngle=-45) to display the
 * overall quality percentage. When breakdown data is provided, additional
 * progress bars show per-dimension scores with their weighted contributions.
 *
 * @example
 * ```tsx
 * <QualityScoreGauge
 *   overallScore={0.97}
 *   breakdown={{
 *     statistical: 0.98,
 *     businessRules: 0.95,
 *     referentialIntegrity: 0.96,
 *   }}
 *   size="lg"
 *   title="Generation Quality"
 *   showBreakdown
 * />
 * ```
 */
const QualityScoreGauge: React.FC<QualityScoreGaugeProps> = ({
  overallScore,
  breakdown,
  size = 'md',
  title,
  showBreakdown = true,
  threshold = 0.95,
  isLoading = false,
  className = '',
}): JSX.Element => {
  // Resolve pixel dimensions from the size prop
  const chartSize: number = SIZE_MAP[size] ?? SIZE_MAP.md;

  // Compute bar thickness based on gauge size
  const barSize: number = size === 'sm' ? 10 : size === 'md' ? 14 : 18;

  // Clamp overallScore to valid range [0, 1] for safety
  const clampedScore: number = Math.min(1, Math.max(0, overallScore));

  // Derive display values from score — memoized to avoid recalculation
  const overallPercentage: number = useMemo(
    () => Math.round(clampedScore * 100),
    [clampedScore],
  );

  const scoreColor: string = useMemo(
    () => getScoreColor(clampedScore),
    [clampedScore],
  );

  const meetsThreshold: boolean = useMemo(
    () => clampedScore >= threshold,
    [clampedScore, threshold],
  );

  // Prepare radial bar chart data for the main gauge arc
  const gaugeData: GaugeDataEntry[] = useMemo(
    () => [
      {
        name: 'Quality Score',
        value: overallPercentage,
        fill: scoreColor,
      },
    ],
    [overallPercentage, scoreColor],
  );

  // Prepare breakdown data for the sub-score visualization arcs
  const breakdownData: BreakdownDataEntry[] = useMemo(() => {
    if (!breakdown) return [];

    // Clamp each sub-score to [0, 1] range
    const clampedStatistical = Math.min(1, Math.max(0, breakdown.statistical));
    const clampedBusinessRules = Math.min(1, Math.max(0, breakdown.businessRules));
    const clampedReferentialIntegrity = Math.min(1, Math.max(0, breakdown.referentialIntegrity));

    return [
      {
        name: BREAKDOWN_LABELS.referentialIntegrity,
        value: Math.round(clampedReferentialIntegrity * 100),
        fill: BREAKDOWN_COLORS.referentialIntegrity,
        weight: '30%',
      },
      {
        name: BREAKDOWN_LABELS.businessRules,
        value: Math.round(clampedBusinessRules * 100),
        fill: BREAKDOWN_COLORS.businessRules,
        weight: '30%',
      },
      {
        name: BREAKDOWN_LABELS.statistical,
        value: Math.round(clampedStatistical * 100),
        fill: BREAKDOWN_COLORS.statistical,
        weight: '40%',
      },
    ];
  }, [breakdown]);

  // ---------- Loading State ----------
  if (isLoading) {
    return (
      <LoadingSkeleton chartSize={chartSize} className={className} title={title} />
    );
  }

  // ---------- Main Render ----------
  return (
    <div className={`bg-white dark:bg-gray-800 rounded-lg shadow-sm p-4 ${className}`}>
      {/* Optional title */}
      {title && (
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2 text-center">
          {title}
        </h3>
      )}

      {/* Gauge chart with center label overlay */}
      <div
        className="relative flex justify-center"
        style={{ width: chartSize, height: chartSize, margin: '0 auto' }}
      >
        <ResponsiveContainer width="100%" height="100%">
          <RadialBarChart
            cx="50%"
            cy="50%"
            innerRadius="60%"
            outerRadius="85%"
            barSize={barSize}
            data={gaugeData}
            startAngle={225}
            endAngle={-45}
          >
            <PolarAngleAxis
              type="number"
              domain={[0, 100]}
              angleAxisId={0}
              tick={false}
            />
            <RadialBar
              background={{ fill: '#E5E7EB' }}
              clockWise
              dataKey="value"
              cornerRadius={6}
            >
              {gaugeData.map((entry: GaugeDataEntry, index: number) => (
                <Cell key={`gauge-cell-${index}`} fill={entry.fill} />
              ))}
            </RadialBar>
            <Tooltip
              content={<CustomGaugeTooltip />}
              wrapperStyle={{ outline: 'none' }}
            />
            {/* Recharts Legend — renders a compact breakdown legend beneath the gauge arc */}
            <Legend
              content={
                breakdownData.length > 0
                  ? () => (
                      <div className="flex flex-wrap justify-center gap-2 mt-0">
                        {breakdownData.map((entry: BreakdownDataEntry) => (
                          <div key={entry.name} className="flex items-center gap-1">
                            <div
                              className="w-2 h-2 rounded-full flex-shrink-0"
                              style={{ backgroundColor: entry.fill }}
                            />
                            <span className="text-[10px] text-gray-500 dark:text-gray-400">
                              {entry.name} ({entry.value}%)
                            </span>
                          </div>
                        ))}
                      </div>
                    )
                  : () => null
              }
              wrapperStyle={{ position: 'relative', marginTop: -4 }}
            />
          </RadialBarChart>
        </ResponsiveContainer>

        {/* Center label overlay positioned absolutely over the chart */}
        <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
          <span
            className="text-3xl font-bold"
            style={{ color: scoreColor }}
            aria-label={`Quality score: ${overallPercentage} percent`}
          >
            {overallPercentage}%
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            Quality Score
          </span>
          {meetsThreshold ? (
            <span className="text-xs text-green-600 dark:text-green-400 mt-1 flex items-center gap-1">
              ✓ Meets Target
            </span>
          ) : (
            <span className="text-xs text-amber-600 dark:text-amber-400 mt-1 flex items-center gap-1">
              ⚠ Below Target ({Math.round(threshold * 100)}%)
            </span>
          )}
        </div>
      </div>

      {/* Threshold indicator text */}
      <div className="text-center mt-2">
        <span className="text-xs text-gray-400">
          Target: ≥{Math.round(threshold * 100)}% |{' '}
          Weighted: {Math.round(SCORE_WEIGHTS.statistical * 100)}% Stat +{' '}
          {Math.round(SCORE_WEIGHTS.businessRules * 100)}% Rules +{' '}
          {Math.round(SCORE_WEIGHTS.referentialIntegrity * 100)}% Integrity
        </span>
      </div>

      {/* Detailed score breakdown with progress bars */}
      {showBreakdown && breakdown && (
        <div className="mt-4 space-y-2">
          <h4 className="text-xs font-medium text-gray-600 dark:text-gray-400 uppercase tracking-wide">
            Score Breakdown
          </h4>
          {Object.entries(BREAKDOWN_LABELS).map(([key, label]) => {
            const rawScore: number =
              breakdown[key as keyof QualityScoreBreakdown];
            // Clamp sub-score for display
            const score: number = Math.min(1, Math.max(0, rawScore));
            const weight: number =
              SCORE_WEIGHTS[key as keyof typeof SCORE_WEIGHTS];
            const color: string =
              BREAKDOWN_COLORS[key as keyof typeof BREAKDOWN_COLORS];
            const percentage: number = Math.round(score * 100);
            const weightedContribution: number = Math.round(
              score * weight * 100,
            );

            return (
              <div key={key} className="flex items-center gap-2">
                <div
                  className="w-3 h-3 rounded-full flex-shrink-0"
                  style={{ backgroundColor: color }}
                  aria-hidden="true"
                />
                <div className="flex-1 min-w-0">
                  <div className="flex justify-between text-xs">
                    <span className="text-gray-700 dark:text-gray-300 truncate">
                      {label}
                    </span>
                    <span className="text-gray-500 dark:text-gray-400 ml-2 whitespace-nowrap">
                      {percentage}%{' '}
                      <span className="text-gray-400">
                        (×{Math.round(weight * 100)}% = {weightedContribution}%)
                      </span>
                    </span>
                  </div>
                  <div className="w-full bg-gray-200 dark:bg-gray-600 rounded-full h-1.5 mt-1">
                    <div
                      className="h-1.5 rounded-full transition-all duration-500"
                      style={{
                        width: `${percentage}%`,
                        backgroundColor: color,
                      }}
                      role="progressbar"
                      aria-valuenow={percentage}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label={`${label}: ${percentage}%`}
                    />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

export default QualityScoreGauge;
