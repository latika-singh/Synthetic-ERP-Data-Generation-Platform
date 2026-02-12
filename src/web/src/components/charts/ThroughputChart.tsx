/**
 * @fileoverview Recharts-based time-series line/area chart component for
 * rendering real-time generation throughput metrics (records per second/minute)
 * over time.
 *
 * Used by:
 * - **JobMonitoring page (S-004)** — real-time job progress visualisation.
 * - **Dashboard page (S-001)** — system-wide throughput overview.
 *
 * Supports configurable time windows, area fill, line styling, custom tooltips,
 * responsive container sizing, optional target reference lines, and cumulative
 * record overlays.  Follows TailwindCSS 4.x styling conventions and integrates
 * with the jobStore Zustand store via {@link ThroughputDataPoint} arrays.
 *
 * @module components/charts/ThroughputChart
 */

import React, { useMemo } from 'react';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  ReferenceLine,
} from 'recharts';
import type { BatchProgress } from '@/types/generation';

// ---------------------------------------------------------------------------
// Public Interfaces
// ---------------------------------------------------------------------------

/**
 * Single data point representing throughput metrics at a given moment in time.
 *
 * Produced by mapping {@link BatchProgress} snapshots (using `current_batch`,
 * `total_batches`, `records_generated`, `records_total`, and `percentage`)
 * into a chart-friendly format.
 */
export interface ThroughputDataPoint {
  /** ISO 8601 timestamp or pre-formatted time label (e.g. "14:32:05") */
  timestamp: string;
  /** Instantaneous records generated per second at this data point */
  recordsPerSecond: number;
  /** Rolling-average records generated per minute at this data point */
  recordsPerMinute: number;
  /** Cumulative total of records generated up to this point */
  cumulativeRecords: number;
}

/**
 * Props accepted by the {@link ThroughputChart} component.
 */
export interface ThroughputChartProps {
  /** Array of throughput data points over time */
  data: ThroughputDataPoint[];
  /** Title displayed above the chart */
  title?: string;
  /** Height of the chart container in pixels @default 300 */
  height?: number;
  /** Which throughput metric to render as the primary area */
  metric?: 'recordsPerSecond' | 'recordsPerMinute';
  /** Horizontal target-throughput reference line value (e.g. 16 667 rec/sec ≈ 1 M/min) */
  targetThroughput?: number;
  /** Whether to overlay the cumulative-records dashed line on a secondary Y axis */
  showCumulative?: boolean;
  /** When `true`, a skeleton loading placeholder is rendered instead of the chart */
  isLoading?: boolean;
  /** Colour palette applied to chart strokes and fills */
  colorTheme?: 'blue' | 'green' | 'purple';
  /** Additional CSS class names forwarded to the outermost container `<div>` */
  className?: string;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Colour palettes keyed by theme name.
 *
 * `fill` values use 8-digit hex notation for 12.5 % alpha transparency so
 * the area gradient remains subtle against both light and dark backgrounds.
 */
const THEME_COLORS: Record<string, { primary: string; fill: string; cumulative: string }> = {
  blue: { primary: '#3B82F6', fill: '#3B82F620', cumulative: '#6366F1' },
  green: { primary: '#10B981', fill: '#10B98120', cumulative: '#14B8A6' },
  purple: { primary: '#8B5CF6', fill: '#8B5CF620', cumulative: '#A78BFA' },
};

/** Human-readable label for each metric key */
const METRIC_LABELS: Record<string, string> = {
  recordsPerSecond: 'Records/sec',
  recordsPerMinute: 'Records/min',
};

// ---------------------------------------------------------------------------
// Helper Utilities
// ---------------------------------------------------------------------------

/**
 * Abbreviate large throughput values for compact axis-tick and tooltip display.
 *
 * - ≥ 1 000 000 → e.g. "1.2M"
 * - ≥ 1 000     → e.g. "12.3K"
 * - Otherwise   → integer string
 */
function formatThroughputValue(value: number): string {
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(1)}K`;
  }
  return Math.round(value).toString();
}

/**
 * Format a throughput value with locale-aware thousand separators and a unit
 * suffix.  Used inside the custom tooltip for maximum readability.
 */
function formatThroughputWithUnit(value: number, metricKey: string): string {
  const formatted = value.toLocaleString(undefined, {
    maximumFractionDigits: 0,
  });
  const unit = metricKey === 'recordsPerSecond' ? 'records/sec' : 'records/min';
  return `${formatted} ${unit}`;
}

/**
 * Extract a short HH:mm:ss time label from a timestamp string.
 *
 * If the value is already in "HH:mm:ss" format it is returned as-is.
 * ISO 8601 strings are parsed and reformatted.  Unparseable values are
 * returned unchanged to avoid runtime errors.
 */
function formatTimestampLabel(raw: string): string {
  // Already short format?
  if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(raw)) {
    return raw;
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return raw;
  }
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

// ---------------------------------------------------------------------------
// Custom Tooltip
// ---------------------------------------------------------------------------

/**
 * Props forwarded to the Recharts custom `<Tooltip content={…} />` renderer.
 *
 * Recharts passes `active`, `payload`, and `label` automatically.
 */
interface CustomTooltipProps {
  active?: boolean;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload?: Array<{ name: string; value: number; dataKey: string; color: string }>;
  label?: string;
  /** Primary metric key so we can attach the correct unit */
  metricKey?: string;
}

/**
 * Styled tooltip rendered on hover over chart data points.
 *
 * Displays the timestamp, primary metric value with units, and (optionally)
 * the cumulative record count.
 */
const CustomTooltipContent: React.FC<CustomTooltipProps> = ({
  active,
  payload,
  label,
  metricKey = 'recordsPerSecond',
}) => {
  if (!active || !payload || payload.length === 0) {
    return null;
  }

  return (
    <div
      className="rounded-lg shadow-lg border border-gray-700 text-xs"
      style={{
        backgroundColor: 'rgba(17, 24, 39, 0.95)',
        padding: '8px 12px',
        minWidth: 160,
      }}
    >
      <p className="font-medium text-gray-300 mb-1">{label}</p>
      {payload.map((entry) => {
        const isCumulative = entry.dataKey === 'cumulativeRecords';
        const displayValue = isCumulative
          ? `${entry.value.toLocaleString()} records`
          : formatThroughputWithUnit(entry.value, metricKey);
        return (
          <p key={entry.dataKey} className="flex items-center gap-1.5 py-0.5">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: entry.color }}
            />
            <span className="text-gray-400">{entry.name}:</span>
            <span className="font-semibold text-white">{displayValue}</span>
          </p>
        );
      })}
    </div>
  );
};

// ---------------------------------------------------------------------------
// ThroughputChart Component
// ---------------------------------------------------------------------------

/**
 * Time-series area chart visualising real-time generation throughput.
 *
 * Renders an interactive Recharts `<AreaChart>` with:
 * - A primary filled area for the chosen `metric` (records/sec or records/min).
 * - An optional dashed overlay for cumulative records on a secondary Y axis.
 * - An optional red reference line indicating the target throughput threshold
 *   (e.g. 1 M records/min ≈ 16 667 records/sec as required by the horizontal
 *   scalability target).
 * - Three summary stat cards (current, peak, average) below the chart.
 *
 * The component handles loading, empty, and error states gracefully and is
 * fully responsive via Recharts' `<ResponsiveContainer>`.
 *
 * @example
 * ```tsx
 * <ThroughputChart
 *   data={throughputData}
 *   title="Generation Throughput"
 *   metric="recordsPerSecond"
 *   targetThroughput={16667}
 *   showCumulative
 *   colorTheme="blue"
 * />
 * ```
 */
const ThroughputChart: React.FC<ThroughputChartProps> = ({
  data,
  title,
  height = 300,
  metric = 'recordsPerSecond',
  targetThroughput,
  showCumulative = false,
  isLoading = false,
  colorTheme = 'blue',
  className = '',
}) => {
  // Resolve colour palette — fall back to blue if an unknown key is provided.
  const colors = THEME_COLORS[colorTheme] ?? THEME_COLORS.blue;

  // ------------------------------------------------------------------
  // Memoised formatted data — map timestamps to short HH:mm:ss labels.
  // ------------------------------------------------------------------
  const formattedData: ThroughputDataPoint[] = useMemo(() => {
    if (!data || data.length === 0) {
      return [];
    }
    return data.map((point) => ({
      ...point,
      timestamp: formatTimestampLabel(point.timestamp),
    }));
  }, [data]);

  // ------------------------------------------------------------------
  // Memoised summary statistics: current, peak, and average values for
  // the selected metric.
  // ------------------------------------------------------------------
  const summaryStats = useMemo(() => {
    if (formattedData.length === 0) {
      return { current: 0, peak: 0, average: 0 };
    }
    const values = formattedData.map((d) => d[metric]);
    const current = values[values.length - 1];
    const peak = Math.max(...values);
    const average = values.reduce((sum, v) => sum + v, 0) / values.length;
    return { current, peak, average };
  }, [formattedData, metric]);

  // ------------------------------------------------------------------
  // Memoised Y-axis domain — ensures the chart captures the full data
  // range plus a small headroom buffer for visual breathing room.
  // ------------------------------------------------------------------
  const yDomain = useMemo<[number, string]>(() => {
    if (formattedData.length === 0) {
      return [0, 'auto'];
    }
    const maxVal = Math.max(...formattedData.map((d) => d[metric]));
    const ceiling = targetThroughput
      ? Math.max(maxVal, targetThroughput) * 1.1
      : maxVal * 1.1;
    // Return 0 as the floor and a rounded ceiling to keep ticks clean
    return [0, 'auto'];
  }, [formattedData, metric, targetThroughput]);

  // ------------------------------------------------------------------
  // Container base classes (shared across loading, empty, and chart states)
  // ------------------------------------------------------------------
  const containerClasses = `bg-white dark:bg-gray-800 rounded-lg shadow-sm p-4 ${className}`.trim();

  // ------------------------------------------------------------------
  // LOADING STATE
  // ------------------------------------------------------------------
  if (isLoading) {
    return (
      <div className={containerClasses}>
        {title && (
          <div className="h-4 w-48 bg-gray-200 dark:bg-gray-700 rounded animate-pulse mb-3" />
        )}
        <div
          className="w-full bg-gray-100 dark:bg-gray-700 rounded animate-pulse"
          style={{ height }}
        />
        <p className="text-xs text-gray-400 dark:text-gray-500 text-center mt-2">
          Loading throughput data…
        </p>
      </div>
    );
  }

  // ------------------------------------------------------------------
  // EMPTY STATE
  // ------------------------------------------------------------------
  if (!data || data.length === 0) {
    return (
      <div className={containerClasses}>
        {title && (
          <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{title}</h3>
        )}
        <div
          className="flex flex-col items-center justify-center text-gray-400 dark:text-gray-500"
          style={{ height }}
        >
          {/* Minimal chart-placeholder icon */}
          <svg
            xmlns="http://www.w3.org/2000/svg"
            className="h-10 w-10 mb-2 opacity-40"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3 13l4-4 4 4 4-8 4 4"
            />
            <path strokeLinecap="round" strokeLinejoin="round" d="M3 17h18" />
          </svg>
          <p className="text-sm">No throughput data available</p>
        </div>
      </div>
    );
  }

  // ------------------------------------------------------------------
  // CHART RENDER
  // ------------------------------------------------------------------
  return (
    <div className={containerClasses}>
      {/* Optional title */}
      {title && (
        <h3 className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{title}</h3>
      )}

      {/* Responsive chart container */}
      <ResponsiveContainer width="100%" height={height}>
        <AreaChart
          data={formattedData}
          margin={{ top: 5, right: 20, left: 10, bottom: 5 }}
        >
          <CartesianGrid strokeDasharray="3 3" className="opacity-30" />

          {/* X axis — time labels */}
          <XAxis
            dataKey="timestamp"
            tick={{ fontSize: 11 }}
            interval="preserveStartEnd"
            className="text-gray-500"
          />

          {/* Primary Y axis — throughput values */}
          <YAxis
            yAxisId="left"
            tick={{ fontSize: 11 }}
            tickFormatter={(value: number) => formatThroughputValue(value)}
            className="text-gray-500"
            domain={yDomain}
          />

          {/* Secondary Y axis for cumulative records (only when enabled) */}
          {showCumulative && (
            <YAxis
              yAxisId="right"
              orientation="right"
              tick={{ fontSize: 11 }}
              tickFormatter={(value: number) => formatThroughputValue(value)}
              className="text-gray-500"
            />
          )}

          {/* Interactive tooltip */}
          <Tooltip
            content={<CustomTooltipContent metricKey={metric} />}
            cursor={{ stroke: colors.primary, strokeWidth: 1, strokeDasharray: '4 4' }}
          />

          <Legend
            wrapperStyle={{ fontSize: 11, paddingTop: 4 }}
          />

          {/* Primary metric area */}
          <Area
            yAxisId="left"
            type="monotone"
            dataKey={metric}
            name={METRIC_LABELS[metric] ?? metric}
            stroke={colors.primary}
            fill={colors.fill}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, fill: colors.primary }}
          />

          {/* Cumulative records overlay (conditional) */}
          {showCumulative && (
            <Area
              yAxisId="right"
              type="monotone"
              dataKey="cumulativeRecords"
              name="Cumulative Records"
              stroke={colors.cumulative}
              fill="none"
              strokeWidth={1.5}
              strokeDasharray="5 5"
              dot={false}
            />
          )}

          {/* Target throughput reference line (conditional) */}
          {targetThroughput !== undefined && targetThroughput > 0 && (
            <ReferenceLine
              yAxisId="left"
              y={targetThroughput}
              stroke="#EF4444"
              strokeDasharray="8 4"
              label={{
                value: 'Target',
                fill: '#EF4444',
                fontSize: 11,
                position: 'right',
              }}
            />
          )}
        </AreaChart>
      </ResponsiveContainer>

      {/* Summary statistics row */}
      <div className="grid grid-cols-3 gap-2 mt-3">
        <div className="text-center p-2 bg-gray-50 dark:bg-gray-700 rounded">
          <p className="text-xs text-gray-500 dark:text-gray-400">Current</p>
          <p className="text-sm font-semibold text-gray-800 dark:text-gray-100">
            {formatThroughputValue(summaryStats.current)}
          </p>
        </div>
        <div className="text-center p-2 bg-gray-50 dark:bg-gray-700 rounded">
          <p className="text-xs text-gray-500 dark:text-gray-400">Peak</p>
          <p className="text-sm font-semibold text-gray-800 dark:text-gray-100">
            {formatThroughputValue(summaryStats.peak)}
          </p>
        </div>
        <div className="text-center p-2 bg-gray-50 dark:bg-gray-700 rounded">
          <p className="text-xs text-gray-500 dark:text-gray-400">Average</p>
          <p className="text-sm font-semibold text-gray-800 dark:text-gray-100">
            {formatThroughputValue(summaryStats.average)}
          </p>
        </div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// BatchProgress → ThroughputDataPoint mapping utility
// ---------------------------------------------------------------------------

/**
 * Convert a {@link BatchProgress} snapshot into a {@link ThroughputDataPoint}.
 *
 * This is a convenience utility for consumers (e.g. jobStore) that receive
 * real-time `BatchProgress` events via WebSocket or polling and need to
 * accumulate them into the `data` array expected by `<ThroughputChart />`.
 *
 * The function reads `current_batch`, `total_batches`, `records_generated`,
 * `records_total`, and `percentage` from the incoming progress object and
 * derives instantaneous throughput by comparing successive snapshots.
 *
 * @param progress  - The latest batch progress event.
 * @param elapsedSeconds - Seconds elapsed since the job (or previous
 *   snapshot) started, used to compute records-per-second.
 * @param prevRecordsGenerated - Records generated as of the previous snapshot
 *   (defaults to `0` for the first data point).
 * @returns A chart-ready data point.
 */
export function batchProgressToDataPoint(
  progress: BatchProgress,
  elapsedSeconds: number,
  prevRecordsGenerated: number = 0,
): ThroughputDataPoint {
  // Access all relevant BatchProgress fields for throughput derivation
  const { current_batch, total_batches, records_generated, records_total, percentage } = progress;

  const deltaRecords = records_generated - prevRecordsGenerated;
  const effectiveElapsed = elapsedSeconds > 0 ? elapsedSeconds : 1;
  const recordsPerSecond = deltaRecords / effectiveElapsed;

  // Project records/minute: if we have enough context (more than first batch)
  // use the actual rate; otherwise estimate from the total job size.
  let recordsPerMinute: number;
  if (current_batch > 1 && elapsedSeconds > 0) {
    recordsPerMinute = recordsPerSecond * 60;
  } else if (records_total > 0 && total_batches > 0) {
    // Estimate from overall progress percentage and elapsed time
    const estimatedTotalSeconds =
      percentage > 0 ? (elapsedSeconds / (percentage / 100)) : elapsedSeconds;
    recordsPerMinute =
      estimatedTotalSeconds > 0 ? (records_total / estimatedTotalSeconds) * 60 : 0;
  } else {
    recordsPerMinute = recordsPerSecond * 60;
  }

  return {
    timestamp: new Date().toISOString(),
    recordsPerSecond: Math.max(0, Math.round(recordsPerSecond)),
    recordsPerMinute: Math.max(0, Math.round(recordsPerMinute)),
    cumulativeRecords: records_generated,
  };
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export default ThroughputChart;
