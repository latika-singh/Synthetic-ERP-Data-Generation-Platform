/**
 * DistributionChart — Recharts-based visualization component for statistical
 * distribution curves of profiled ERP column data.
 *
 * Supports seven distribution types detected by the SciPy/NumPy profiling
 * engine on the backend:
 *   - Continuous: normal, log-normal, uniform, exponential
 *   - Discrete:   Poisson, binomial
 *   - Categorical: categorical (rendered as bar chart)
 *
 * Used by:
 *   - ProfileViewer page (Screen S-006) for column-level statistical views
 *   - GenerationWizard page (Screen S-002) for source data distribution preview
 *
 * Consumes ColumnStatistics / Distribution types from @/types/profile.ts
 * and renders using Recharts 2.x AreaChart (continuous) or BarChart (categorical).
 */

import React, { useMemo } from 'react';
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  Cell,
} from 'recharts';
import type {
  DistributionType,
  Distribution,
  ColumnStatistics,
} from '@/types/profile';

// ---------------------------------------------------------------------------
// Exported Interfaces
// ---------------------------------------------------------------------------

/** Represents a single data point on the distribution curve. */
export interface DistributionDataPoint {
  /** x-axis value (numeric for continuous, category label for categorical) */
  x: number | string;
  /** y-axis value (probability density or frequency) */
  y: number;
  /** Optional display label for tooltip */
  label?: string;
}

/** Props accepted by the {@link DistributionChart} component. */
export interface DistributionChartProps {
  /** Column statistics containing distribution data */
  columnStats: ColumnStatistics;
  /** Height of the chart container in pixels @default 250 */
  height?: number;
  /** Width of the chart or "100%" for responsive @default "100%" */
  width?: number | string;
  /** Title displayed above the chart (defaults to column_name) */
  title?: string;
  /** Whether to show the parameter details panel below the chart @default true */
  showParameters?: boolean;
  /** Whether to show fit score indicator @default true */
  showFitScore?: boolean;
  /** Number of data points to generate for continuous distributions @default 100 */
  resolution?: number;
  /** Color for the primary distribution curve/bars */
  color?: string;
  /** Whether the component is in a loading state @default false */
  isLoading?: boolean;
  /** Optional CSS class name for the container */
  className?: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Default colors for each distribution type (TailwindCSS-aligned palette). */
const DISTRIBUTION_COLORS: Record<string, string> = {
  normal: '#3B82F6',       // blue-500
  log_normal: '#8B5CF6',   // violet-500
  poisson: '#06B6D4',      // cyan-500
  uniform: '#10B981',      // emerald-500
  categorical: '#F59E0B',  // amber-500
  exponential: '#EC4899',  // pink-500
  binomial: '#6366F1',     // indigo-500
  unknown: '#6B7280',      // gray-500
};

/** Human-readable distribution type labels. */
const DISTRIBUTION_LABELS: Record<string, string> = {
  normal: 'Normal (Gaussian)',
  log_normal: 'Log-Normal',
  poisson: 'Poisson',
  uniform: 'Uniform',
  categorical: 'Categorical',
  exponential: 'Exponential',
  binomial: 'Binomial',
  unknown: 'Unknown',
};

// ---------------------------------------------------------------------------
// Mathematical Helpers
// ---------------------------------------------------------------------------

/**
 * Compute ln(Γ(n)) using the Stirling/Lanczos approximation.
 * Required for binomial coefficient calculations in log-space to avoid
 * overflow for large n values.
 */
function logGamma(z: number): number {
  if (z < 0.5) {
    // Reflection formula
    return Math.log(Math.PI / Math.sin(Math.PI * z)) - logGamma(1 - z);
  }
  z -= 1;
  const coefficients = [
    76.18009172947146, -86.50532032941677, 24.01409824083091,
    -1.231739572450155, 0.1208650973866179e-2, -0.5395239384953e-5,
  ];
  let x = 0.99999999999980993;
  for (let i = 0; i < coefficients.length; i++) {
    x += coefficients[i] / (z + 1 + i);
  }
  const t = z + coefficients.length - 0.5;
  return 0.5 * Math.log(2 * Math.PI) + (z + 0.5) * Math.log(t) - t + Math.log(x);
}

/** Compute ln(n!) using the logGamma function: ln(n!) = ln(Γ(n+1)). */
function logFactorial(n: number): number {
  if (n <= 1) return 0;
  return logGamma(n + 1);
}

/** Compute log of binomial coefficient C(n, k) = n! / (k! * (n-k)!). */
function logBinomCoeff(n: number, k: number): number {
  if (k < 0 || k > n) return -Infinity;
  return logFactorial(n) - logFactorial(k) - logFactorial(n - k);
}

// ---------------------------------------------------------------------------
// Distribution Data Generators
// ---------------------------------------------------------------------------

/**
 * Generate probability density values for a Normal (Gaussian) distribution.
 * PDF: f(x) = (1 / (σ√(2π))) · exp(-0.5 · ((x - μ) / σ)²)
 * Range: [μ - 4σ, μ + 4σ] covers > 99.99% of the distribution.
 */
function generateNormalData(
  mean: number,
  stdDev: number,
  resolution: number,
): DistributionDataPoint[] {
  if (stdDev <= 0) return [];
  const points: DistributionDataPoint[] = [];
  const xMin = mean - 4 * stdDev;
  const xMax = mean + 4 * stdDev;
  const step = (xMax - xMin) / (resolution - 1);
  const coeff = 1 / (stdDev * Math.sqrt(2 * Math.PI));

  for (let i = 0; i < resolution; i++) {
    const x = xMin + i * step;
    const z = (x - mean) / stdDev;
    const y = coeff * Math.exp(-0.5 * z * z);
    points.push({ x: parseFloat(x.toFixed(6)), y });
  }
  return points;
}

/**
 * Generate probability density values for a Log-Normal distribution.
 * PDF: f(x) = (1 / (xσ√(2π))) · exp(-0.5 · ((ln(x) - μ) / σ)²), x > 0
 * Range: [0.001, exp(μ + 4σ)] covers the practical domain.
 */
function generateLogNormalData(
  mu: number,
  sigma: number,
  resolution: number,
): DistributionDataPoint[] {
  if (sigma <= 0) return [];
  const points: DistributionDataPoint[] = [];
  const xMin = 0.001;
  const xMax = Math.exp(mu + 4 * sigma);
  const step = (xMax - xMin) / (resolution - 1);
  const coeff = 1 / (sigma * Math.sqrt(2 * Math.PI));

  for (let i = 0; i < resolution; i++) {
    const x = xMin + i * step;
    if (x <= 0) continue;
    const logX = Math.log(x);
    const z = (logX - mu) / sigma;
    const y = (coeff / x) * Math.exp(-0.5 * z * z);
    points.push({ x: parseFloat(x.toFixed(6)), y });
  }
  return points;
}

/**
 * Generate probability mass values for a Poisson distribution.
 * PMF: P(X = k) = (λ^k · e^(-λ)) / k!  (computed in log-space)
 * Range: [0, min(λ + 4√λ, 50)] covering the practical support.
 */
function generatePoissonData(lambda: number): DistributionDataPoint[] {
  if (lambda <= 0) return [];
  const points: DistributionDataPoint[] = [];
  const kMax = Math.min(Math.ceil(lambda + 4 * Math.sqrt(lambda)), 50);

  for (let k = 0; k <= kMax; k++) {
    // log(P(k)) = k·ln(λ) - λ - ln(k!)
    const logP = k * Math.log(lambda) - lambda - logFactorial(k);
    const y = Math.exp(logP);
    points.push({ x: k, y, label: `k = ${k}` });
  }
  return points;
}

/**
 * Generate probability density values for a Uniform distribution.
 * PDF: f(x) = 1 / (max - min) for x ∈ [min, max], else 0
 * Adds zero-height endpoints just outside the range for visual clarity.
 */
function generateUniformData(
  min: number,
  max: number,
  resolution: number,
): DistributionDataPoint[] {
  if (max <= min) return [];
  const density = 1 / (max - min);
  const points: DistributionDataPoint[] = [];
  const margin = (max - min) * 0.05;

  // Zero-height leading edge
  points.push({ x: parseFloat((min - margin).toFixed(6)), y: 0 });
  points.push({ x: parseFloat(min.toFixed(6)), y: density });

  const innerPoints = Math.max(resolution - 4, 2);
  const step = (max - min) / (innerPoints + 1);
  for (let i = 1; i <= innerPoints; i++) {
    const x = min + i * step;
    points.push({ x: parseFloat(x.toFixed(6)), y: density });
  }

  // Zero-height trailing edge
  points.push({ x: parseFloat(max.toFixed(6)), y: density });
  points.push({ x: parseFloat((max + margin).toFixed(6)), y: 0 });

  return points;
}

/**
 * Generate frequency data for a Categorical distribution.
 * Parameters are expected as { category_name: frequency_or_probability }.
 * Sorted descending by frequency, limited to top 20 categories.
 */
function generateCategoricalData(
  parameters: Record<string, number>,
): DistributionDataPoint[] {
  const entries = Object.entries(parameters)
    // Exclude meta-parameters that are not actual categories
    .filter(([key]) => key !== 'category_count')
    .map(([category, frequency]) => ({
      x: category as string | number,
      y: frequency,
      label: category,
    }))
    .sort((a, b) => b.y - a.y);

  return entries.slice(0, 20);
}

/**
 * Generate probability density values for an Exponential distribution.
 * PDF: f(x) = λ · e^(-λx), x ≥ 0
 * Range: [0, 5/λ] covers ~99.3% of the distribution.
 */
function generateExponentialData(
  lambda: number,
  resolution: number,
): DistributionDataPoint[] {
  if (lambda <= 0) return [];
  const points: DistributionDataPoint[] = [];
  const xMax = 5 / lambda;
  const step = xMax / (resolution - 1);

  for (let i = 0; i < resolution; i++) {
    const x = i * step;
    const y = lambda * Math.exp(-lambda * x);
    points.push({ x: parseFloat(x.toFixed(6)), y });
  }
  return points;
}

/**
 * Generate probability mass values for a Binomial distribution.
 * PMF: P(X = k) = C(n,k) · p^k · (1-p)^(n-k)  (computed in log-space)
 * Range: [0, n]
 */
function generateBinomialData(n: number, p: number): DistributionDataPoint[] {
  if (n <= 0 || p < 0 || p > 1) return [];
  const points: DistributionDataPoint[] = [];
  const nInt = Math.round(n);

  for (let k = 0; k <= nInt; k++) {
    let logP = logBinomCoeff(nInt, k);
    if (p > 0) logP += k * Math.log(p);
    else if (k > 0) logP = -Infinity;
    if (p < 1) logP += (nInt - k) * Math.log(1 - p);
    else if (k < nInt) logP = -Infinity;
    const y = Math.exp(logP);
    points.push({ x: k, y, label: `k = ${k}` });
  }
  return points;
}

/**
 * Master dispatcher — selects the correct generator based on distribution type
 * and extracts the relevant parameters from the distribution object.
 *
 * Handles both `low/high` (profile.ts convention) and `min/max` param names
 * for uniform distributions to maintain compatibility with varying backend outputs.
 */
function generateDistributionData(
  distribution: Distribution,
  resolution: number,
): DistributionDataPoint[] {
  const params = distribution.parameters;
  const distType = distribution.type as string;

  switch (distType) {
    case 'normal':
      return generateNormalData(
        params.mean ?? 0,
        params.std_dev ?? 1,
        resolution,
      );

    case 'log_normal':
      return generateLogNormalData(
        params.mu ?? 0,
        params.sigma ?? 1,
        resolution,
      );

    case 'poisson':
      return generatePoissonData(params.lambda ?? 1);

    case 'uniform':
      return generateUniformData(
        params.low ?? params.min ?? 0,
        params.high ?? params.max ?? 1,
        resolution,
      );

    case 'categorical':
      return generateCategoricalData(params);

    case 'exponential':
      return generateExponentialData(params.lambda ?? 1, resolution);

    case 'binomial':
      return generateBinomialData(params.n ?? 10, params.p ?? 0.5);

    case 'unknown':
    default:
      return [];
  }
}

// ---------------------------------------------------------------------------
// Custom Tooltip Components
// ---------------------------------------------------------------------------

/** Payload shape provided by Recharts for each series in the tooltip. */
interface TooltipPayloadEntry {
  value: number;
  name: string;
  color?: string;
  payload: DistributionDataPoint;
}

/** Props received by custom Recharts tooltip renderers. */
interface CustomTooltipProps {
  active?: boolean;
  payload?: TooltipPayloadEntry[];
  label?: string | number;
}

/** Tooltip for continuous distribution charts (AreaChart). */
function ContinuousTooltip({
  active,
  payload,
  distributionType,
}: CustomTooltipProps & { distributionType: string }): React.ReactElement | null {
  if (!active || !payload || payload.length === 0) return null;

  const entry = payload[0];
  const xValue = typeof entry.payload.x === 'number'
    ? entry.payload.x.toFixed(4)
    : String(entry.payload.x);
  const yValue = entry.value < 0.001
    ? entry.value.toExponential(3)
    : entry.value.toFixed(6);

  return (
    <div className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg px-3 py-2 text-xs">
      <p className="font-medium text-gray-700 dark:text-gray-300 mb-1">
        {DISTRIBUTION_LABELS[distributionType] ?? distributionType}
      </p>
      <p className="text-gray-600 dark:text-gray-400">
        <span className="font-medium">x:</span> {xValue}
      </p>
      <p className="text-gray-600 dark:text-gray-400">
        <span className="font-medium">Density:</span> {yValue}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

/**
 * Recharts-based React component for rendering statistical distribution curves
 * of profiled ERP column data. Renders continuous distributions as smooth
 * area curves and categorical distributions as bar charts.
 */
const DistributionChart: React.FC<DistributionChartProps> = ({
  columnStats,
  height = 250,
  width: _width,
  title,
  showParameters = true,
  showFitScore = true,
  resolution = 100,
  color: colorProp,
  isLoading = false,
  className = '',
}) => {
  // Derive distribution metadata
  const distributionType: string = columnStats.distribution.type as string;
  const color = colorProp ?? DISTRIBUTION_COLORS[distributionType] ?? DISTRIBUTION_COLORS.unknown;
  const isCategorical = distributionType === 'categorical';

  // Memoize chart data generation — expensive math should only recalculate
  // when distribution or resolution change, not on every render.
  const chartData: DistributionDataPoint[] = useMemo(
    () => generateDistributionData(columnStats.distribution, resolution),
    [columnStats.distribution, resolution],
  );

  // Compute total frequency for categorical percentage display in tooltip
  const categoricalTotal = useMemo(() => {
    if (!isCategorical) return 0;
    return chartData.reduce((sum, point) => sum + point.y, 0);
  }, [isCategorical, chartData]);

  // --- Loading skeleton ---
  if (isLoading) {
    return (
      <div
        className={`bg-white dark:bg-gray-800 rounded-lg shadow-sm p-4 ${className}`}
        role="status"
        aria-label="Loading distribution chart"
      >
        <div className="animate-pulse">
          <div className="h-4 w-1/3 bg-gray-200 dark:bg-gray-700 rounded mb-3" />
          <div className="h-3 w-1/4 bg-gray-200 dark:bg-gray-700 rounded mb-4" />
          <div
            className="bg-gray-100 dark:bg-gray-700 rounded"
            style={{ height }}
          />
          {showParameters && (
            <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
              {Array.from({ length: 4 }).map((_, i) => (
                <div
                  key={`skel-${i}`}
                  className="h-12 bg-gray-100 dark:bg-gray-700 rounded"
                />
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  // --- Empty / Unknown state ---
  if (
    distributionType === 'unknown' ||
    chartData.length === 0
  ) {
    return (
      <div
        className={`bg-white dark:bg-gray-800 rounded-lg shadow-sm p-4 ${className}`}
      >
        {(title || columnStats.column_name) && (
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            {title || columnStats.column_name}
            <span className="ml-2 text-xs text-gray-400 font-normal">
              ({columnStats.data_type})
            </span>
          </h3>
        )}
        <div
          className="flex items-center justify-center text-sm text-gray-400 dark:text-gray-500"
          style={{ height }}
        >
          Distribution data not available for this column
        </div>
      </div>
    );
  }

  // --- Categorical Tooltip with percentage (wraps base CategoricalTooltip) ---
  const CategoricalTooltipWithTotal: React.FC<CustomTooltipProps> = (props) => {
    if (!props.active || !props.payload || props.payload.length === 0) return null;
    const entry = props.payload[0];
    const category = entry.payload.label ?? String(entry.payload.x);
    const frequency = entry.value;
    const pct = categoricalTotal > 0 ? ((frequency / categoricalTotal) * 100).toFixed(1) : '0.0';

    return (
      <div className="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg px-3 py-2 text-xs">
        <p className="font-medium text-gray-700 dark:text-gray-300 mb-1 truncate max-w-[180px]">
          {category}
        </p>
        <p className="text-gray-600 dark:text-gray-400">
          <span className="font-medium">Frequency:</span>{' '}
          {frequency.toLocaleString()}
        </p>
        <p className="text-gray-600 dark:text-gray-400">
          <span className="font-medium">Percentage:</span> {pct}%
        </p>
      </div>
    );
  };

  // --- Render ---
  return (
    <div className={`bg-white dark:bg-gray-800 rounded-lg shadow-sm p-4 ${className}`}>
      {/* Title */}
      {(title || columnStats.column_name) && (
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          {title || columnStats.column_name}
          <span className="ml-2 text-xs text-gray-400 font-normal">
            ({columnStats.data_type})
          </span>
        </h3>
      )}

      {/* Distribution type badge + fit score */}
      <div className="flex items-center gap-2 mb-2">
        <span
          className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium"
          style={{ backgroundColor: `${color}20`, color }}
        >
          {DISTRIBUTION_LABELS[distributionType] ?? distributionType}
        </span>
        {showFitScore &&
          columnStats.distribution.fit_score != null && (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              Fit: {(columnStats.distribution.fit_score * 100).toFixed(1)}%
            </span>
          )}
      </div>

      {/* Chart — categorical renders BarChart, all others render AreaChart */}
      {isCategorical ? (
        <ResponsiveContainer width="100%" height={height}>
          <BarChart
            data={chartData}
            margin={{ top: 5, right: 20, left: 10, bottom: 5 }}
          >
            <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
            <XAxis
              dataKey="x"
              tick={{ fontSize: 10, angle: -35, textAnchor: 'end' } as object}
              height={60}
              interval={0}
            />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip content={<CategoricalTooltipWithTotal />} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Bar dataKey="y" name="Frequency" radius={[4, 4, 0, 0]}>
              {chartData.map((_entry, index) => (
                <Cell
                  key={`cell-${index}`}
                  fill={color}
                  fillOpacity={Math.max(0.3, 0.8 - index * 0.02)}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          <AreaChart
            data={chartData}
            margin={{ top: 5, right: 20, left: 10, bottom: 5 }}
          >
            <CartesianGrid strokeDasharray="3 3" className="opacity-30" />
            <XAxis
              dataKey="x"
              type="number"
              tick={{ fontSize: 11 }}
              tickFormatter={(value: number) => value.toFixed(1)}
              domain={['dataMin', 'dataMax']}
            />
            <YAxis
              tick={{ fontSize: 11 }}
              tickFormatter={(value: number) =>
                value < 0.01 ? value.toExponential(1) : value.toFixed(3)
              }
            />
            <Tooltip
              content={
                <ContinuousTooltip distributionType={distributionType} />
              }
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Area
              type="monotone"
              dataKey="y"
              name="Probability Density"
              stroke={color}
              fill={color}
              fillOpacity={0.2}
              strokeWidth={2}
              dot={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      )}

      {/* Parameter details panel */}
      {showParameters && (
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs">
          {columnStats.mean != null && (
            <div className="bg-gray-50 dark:bg-gray-700 rounded p-2">
              <span className="text-gray-500 dark:text-gray-400 block">
                Mean
              </span>
              <span className="font-medium text-gray-800 dark:text-gray-200">
                {columnStats.mean.toLocaleString()}
              </span>
            </div>
          )}
          {columnStats.std_dev != null && (
            <div className="bg-gray-50 dark:bg-gray-700 rounded p-2">
              <span className="text-gray-500 dark:text-gray-400 block">
                Std Dev
              </span>
              <span className="font-medium text-gray-800 dark:text-gray-200">
                {columnStats.std_dev.toLocaleString()}
              </span>
            </div>
          )}
          {columnStats.min_value != null && (
            <div className="bg-gray-50 dark:bg-gray-700 rounded p-2">
              <span className="text-gray-500 dark:text-gray-400 block">
                Min
              </span>
              <span className="font-medium text-gray-800 dark:text-gray-200">
                {columnStats.min_value.toLocaleString()}
              </span>
            </div>
          )}
          {columnStats.max_value != null && (
            <div className="bg-gray-50 dark:bg-gray-700 rounded p-2">
              <span className="text-gray-500 dark:text-gray-400 block">
                Max
              </span>
              <span className="font-medium text-gray-800 dark:text-gray-200">
                {columnStats.max_value.toLocaleString()}
              </span>
            </div>
          )}
          <div className="bg-gray-50 dark:bg-gray-700 rounded p-2">
            <span className="text-gray-500 dark:text-gray-400 block">
              Null %
            </span>
            <span className="font-medium text-gray-800 dark:text-gray-200">
              {columnStats.null_percentage.toFixed(1)}%
            </span>
          </div>
          {columnStats.unique_count != null && (
            <div className="bg-gray-50 dark:bg-gray-700 rounded p-2">
              <span className="text-gray-500 dark:text-gray-400 block">
                Unique
              </span>
              <span className="font-medium text-gray-800 dark:text-gray-200">
                {columnStats.unique_count.toLocaleString()}
              </span>
            </div>
          )}
          {/* Distribution-specific parameters */}
          {Object.entries(columnStats.distribution.parameters).map(
            ([key, value]) => (
              <div
                key={key}
                className="bg-gray-50 dark:bg-gray-700 rounded p-2"
              >
                <span className="text-gray-500 dark:text-gray-400 block capitalize">
                  {key.replace(/_/g, ' ')}
                </span>
                <span className="font-medium text-gray-800 dark:text-gray-200">
                  {typeof value === 'number'
                    ? value.toLocaleString(undefined, {
                        maximumFractionDigits: 4,
                      })
                    : String(value)}
                </span>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
};

export default DistributionChart;
