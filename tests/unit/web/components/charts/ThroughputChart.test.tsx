/**
 * @fileoverview Comprehensive Vitest + React Testing Library unit tests for the
 * ThroughputChart Recharts time-series area chart component.
 *
 * Tests cover:
 * - AreaChart rendering with proper data binding and Recharts sub-components
 * - Records/second and records/minute metric display switching
 * - Cumulative records overlay line toggling with dual Y-axis
 * - Target throughput ReferenceLine rendering with label
 * - formatThroughputValue helper function (K suffix for thousands, M for millions)
 * - Summary stats row displaying current/peak/average throughput values
 * - Loading skeleton state with pulse animation
 * - Empty data state with informational message
 * - Color theme variants (blue, green, purple) applying correct stroke and fill
 * - Title display and custom height/className props
 *
 * All Recharts components are mocked with functional component stubs that render
 * testable DOM elements with data-testid attributes for assertion.
 *
 * @module tests/unit/web/components/charts/ThroughputChart.test
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import ThroughputChart from '@/components/charts/ThroughputChart';
import type { ThroughputDataPoint } from '@/components/charts/ThroughputChart';

// ---------------------------------------------------------------------------
// Recharts Module Mock
// ---------------------------------------------------------------------------
// Replace all Recharts components with lightweight DOM stubs that expose
// chart configuration via data-* attributes for assertion. This avoids
// SVG rendering complexity in jsdom and allows direct prop verification.
// ---------------------------------------------------------------------------

vi.mock('recharts', () => ({
  /**
   * ResponsiveContainer mock — renders a div wrapper with data-testid and
   * data-height attributes, forwarding children so nested chart elements
   * are rendered into the DOM.
   */
  ResponsiveContainer: ({ children, height, ...props }: any) => (
    <div data-testid="responsive-container" data-height={height}>
      {children}
    </div>
  ),

  /**
   * AreaChart mock — renders a div container with data-testid, forwarding
   * children (Area, XAxis, YAxis, etc.) into the DOM for individual assertion.
   */
  AreaChart: ({ children, ...props }: any) => (
    <div data-testid="area-chart">{children}</div>
  ),

  /**
   * Area mock — renders a div with data-testid and exposes dataKey, name,
   * stroke, and fill as data attributes for verifying metric binding and
   * color theme application.
   */
  Area: ({ dataKey, name, stroke, fill, ...props }: any) => (
    <div
      data-testid="area"
      data-datakey={dataKey}
      data-name={name}
      data-stroke={stroke}
      data-fill={fill}
    />
  ),

  /**
   * XAxis mock — renders a div with data-testid for presence verification.
   */
  XAxis: (props: any) => <div data-testid="x-axis" />,

  /**
   * YAxis mock — renders a div with data-testid and data-yaxisid to verify
   * left/right axis configuration for dual-axis cumulative overlay.
   */
  YAxis: ({ yAxisId, ...props }: any) => (
    <div data-testid="y-axis" data-yaxisid={yAxisId} />
  ),

  /**
   * CartesianGrid mock — renders a div with data-testid for presence check.
   */
  CartesianGrid: (props: any) => <div data-testid="cartesian-grid" />,

  /**
   * Tooltip mock — renders a div with data-testid for presence check.
   */
  Tooltip: (props: any) => <div data-testid="tooltip" />,

  /**
   * Legend mock — renders a div with data-testid for presence check.
   */
  Legend: (props: any) => <div data-testid="legend" />,

  /**
   * ReferenceLine mock — renders a div with data-testid, data-y for the
   * threshold value, and renders the label text content when the label
   * prop is an object with a `value` key (as used by the component for
   * the "Target" label).
   */
  ReferenceLine: ({ y, label, ...props }: any) => (
    <div data-testid="reference-line" data-y={String(y)}>
      {label && typeof label === 'object' && 'value' in label
        ? label.value
        : typeof label === 'string'
          ? label
          : null}
    </div>
  ),
}));

// ---------------------------------------------------------------------------
// Test Data Fixtures
// ---------------------------------------------------------------------------

/**
 * Primary mock dataset: 10 time-series data points spanning 1.5 minutes.
 *
 * Key statistical properties for assertion:
 * - recordsPerSecond values: [5000, 8500, 12000, 15500, 16700, 14200, 17800, 16000, 18200, 17500]
 * - Current (last): 17500  → formatted: "17.5K"
 * - Peak (max): 18200      → formatted: "18.2K"
 * - Average: 14140          → formatted: "14.1K"
 * - recordsPerMinute values include values above 1M (1002000, 1068000, 1092000, 1050000)
 * - cumulativeRecords monotonically increasing from 50000 to 1414400
 */
const mockThroughputData: ThroughputDataPoint[] = [
  { timestamp: '14:30:00', recordsPerSecond: 5000, recordsPerMinute: 300000, cumulativeRecords: 50000 },
  { timestamp: '14:30:10', recordsPerSecond: 8500, recordsPerMinute: 510000, cumulativeRecords: 135000 },
  { timestamp: '14:30:20', recordsPerSecond: 12000, recordsPerMinute: 720000, cumulativeRecords: 255000 },
  { timestamp: '14:30:30', recordsPerSecond: 15500, recordsPerMinute: 930000, cumulativeRecords: 410500 },
  { timestamp: '14:30:40', recordsPerSecond: 16700, recordsPerMinute: 1002000, cumulativeRecords: 577200 },
  { timestamp: '14:30:50', recordsPerSecond: 14200, recordsPerMinute: 852000, cumulativeRecords: 719400 },
  { timestamp: '14:31:00', recordsPerSecond: 17800, recordsPerMinute: 1068000, cumulativeRecords: 897200 },
  { timestamp: '14:31:10', recordsPerSecond: 16000, recordsPerMinute: 960000, cumulativeRecords: 1057200 },
  { timestamp: '14:31:20', recordsPerSecond: 18200, recordsPerMinute: 1092000, cumulativeRecords: 1239400 },
  { timestamp: '14:31:30', recordsPerSecond: 17500, recordsPerMinute: 1050000, cumulativeRecords: 1414400 },
];

/** Empty dataset for testing the empty state rendering path */
const emptyData: ThroughputDataPoint[] = [];

/** Single data point for edge-case testing (current = peak = average) */
const singlePointData: ThroughputDataPoint[] = [
  { timestamp: '14:30:00', recordsPerSecond: 10000, recordsPerMinute: 600000, cumulativeRecords: 10000 },
];

// ---------------------------------------------------------------------------
// Test Suites
// ---------------------------------------------------------------------------

describe('ThroughputChart', () => {
  /**
   * Reset all mock function call counts and implementations between tests
   * to prevent cross-test contamination.
   */
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // =========================================================================
  // Time-series AreaChart rendering
  // =========================================================================
  describe('time-series AreaChart rendering', () => {
    it('renders AreaChart with data', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
    });

    it('renders CartesianGrid for visual reference', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByTestId('cartesian-grid')).toBeInTheDocument();
    });

    it('renders XAxis for time labels', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByTestId('x-axis')).toBeInTheDocument();
    });

    it('renders YAxis for throughput values', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByTestId('y-axis')).toBeInTheDocument();
    });

    it('renders Tooltip for interactive hover data', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByTestId('tooltip')).toBeInTheDocument();
    });

    it('renders Legend component', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByTestId('legend')).toBeInTheDocument();
    });

    it('wraps chart in ResponsiveContainer', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Records/second and records/minute metric display
  // =========================================================================
  describe('records/second and records/minute metric display', () => {
    it('renders recordsPerSecond metric by default', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      const areaEl = screen.getByTestId('area');
      expect(areaEl).toHaveAttribute('data-datakey', 'recordsPerSecond');
    });

    it('renders recordsPerSecond with correct name label', () => {
      render(<ThroughputChart data={mockThroughputData} metric="recordsPerSecond" />);
      const areaEl = screen.getByTestId('area');
      expect(areaEl).toHaveAttribute('data-name', 'Records/sec');
    });

    it('renders recordsPerMinute metric when specified', () => {
      render(<ThroughputChart data={mockThroughputData} metric="recordsPerMinute" />);
      const areaEl = screen.getByTestId('area');
      expect(areaEl).toHaveAttribute('data-datakey', 'recordsPerMinute');
    });

    it('renders recordsPerMinute with correct name label', () => {
      render(<ThroughputChart data={mockThroughputData} metric="recordsPerMinute" />);
      const areaEl = screen.getByTestId('area');
      expect(areaEl).toHaveAttribute('data-name', 'Records/min');
    });
  });

  // =========================================================================
  // Cumulative records overlay line
  // =========================================================================
  describe('cumulative records overlay line', () => {
    it('does not render cumulative overlay by default', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      const areas = screen.getAllByTestId('area');
      // Only the primary metric area should be rendered
      expect(areas).toHaveLength(1);
    });

    it('renders cumulative records overlay when showCumulative is true', () => {
      render(<ThroughputChart data={mockThroughputData} showCumulative={true} />);
      const areas = screen.getAllByTestId('area');
      // Primary metric area + cumulative records overlay
      expect(areas.length).toBeGreaterThan(1);
    });

    it('renders second YAxis for cumulative overlay (right orientation)', () => {
      render(<ThroughputChart data={mockThroughputData} showCumulative={true} />);
      const yAxes = screen.getAllByTestId('y-axis');
      // Left axis (primary) + right axis (cumulative)
      expect(yAxes.length).toBeGreaterThan(1);
    });

    it('does not render second YAxis when showCumulative is false', () => {
      render(<ThroughputChart data={mockThroughputData} showCumulative={false} />);
      const yAxes = screen.getAllByTestId('y-axis');
      // Only the primary left Y-axis
      expect(yAxes).toHaveLength(1);
    });
  });

  // =========================================================================
  // Target throughput ReferenceLine
  // =========================================================================
  describe('target throughput ReferenceLine', () => {
    it('renders ReferenceLine when targetThroughput is provided', () => {
      render(<ThroughputChart data={mockThroughputData} targetThroughput={16667} />);
      expect(screen.getByTestId('reference-line')).toBeInTheDocument();
    });

    it('does not render ReferenceLine when targetThroughput is not provided', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.queryByTestId('reference-line')).not.toBeInTheDocument();
    });

    it('passes correct y value to ReferenceLine', () => {
      render(<ThroughputChart data={mockThroughputData} targetThroughput={16667} />);
      const refLine = screen.getByTestId('reference-line');
      expect(refLine).toHaveAttribute('data-y', '16667');
    });

    it('displays "Target" label on ReferenceLine', () => {
      render(<ThroughputChart data={mockThroughputData} targetThroughput={16667} />);
      expect(screen.getByText('Target')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // formatThroughputValue helper
  // =========================================================================
  // formatThroughputValue is an internal (non-exported) helper. Its behaviour
  // is verified indirectly through the summary stats row, which formats the
  // current, peak, and average values using the helper before rendering them.
  // Each test creates a focused dataset where current = peak = average = the
  // test value, so the formatted output appears in all three stat cards.
  // =========================================================================
  describe('formatThroughputValue helper', () => {
    it('formats values ≥1,000,000 with M suffix (e.g., 1.5M)', () => {
      const data: ThroughputDataPoint[] = [
        { timestamp: '14:30:00', recordsPerSecond: 1500000, recordsPerMinute: 90000000, cumulativeRecords: 1500000 },
      ];
      render(<ThroughputChart data={data} />);
      // 1500000 / 1_000_000 = 1.5 → "1.5M"
      const formatted = screen.getAllByText('1.5M');
      expect(formatted.length).toBeGreaterThan(0);
    });

    it('formats values ≥1,000 with K suffix (e.g., 12.3K)', () => {
      const data: ThroughputDataPoint[] = [
        { timestamp: '14:30:00', recordsPerSecond: 12300, recordsPerMinute: 738000, cumulativeRecords: 12300 },
      ];
      render(<ThroughputChart data={data} />);
      // 12300 / 1_000 = 12.3 → "12.3K"
      const formatted = screen.getAllByText('12.3K');
      expect(formatted.length).toBeGreaterThan(0);
    });

    it('formats values <1,000 as plain integers', () => {
      const data: ThroughputDataPoint[] = [
        { timestamp: '14:30:00', recordsPerSecond: 500, recordsPerMinute: 30000, cumulativeRecords: 500 },
      ];
      render(<ThroughputChart data={data} />);
      // 500 < 1000 → Math.round(500).toString() = "500"
      const formatted = screen.getAllByText('500');
      expect(formatted.length).toBeGreaterThan(0);
    });

    it('formats exactly 1,000,000 as 1.0M', () => {
      const data: ThroughputDataPoint[] = [
        { timestamp: '14:30:00', recordsPerSecond: 1000000, recordsPerMinute: 60000000, cumulativeRecords: 1000000 },
      ];
      render(<ThroughputChart data={data} />);
      // 1000000 / 1_000_000 = 1.0 → "1.0M"
      const formatted = screen.getAllByText('1.0M');
      expect(formatted.length).toBeGreaterThan(0);
    });

    it('formats exactly 1,000 as 1.0K', () => {
      const data: ThroughputDataPoint[] = [
        { timestamp: '14:30:00', recordsPerSecond: 1000, recordsPerMinute: 60000, cumulativeRecords: 1000 },
      ];
      render(<ThroughputChart data={data} />);
      // 1000 / 1_000 = 1.0 → "1.0K"
      const formatted = screen.getAllByText('1.0K');
      expect(formatted.length).toBeGreaterThan(0);
    });

    it('formats 0 as 0', () => {
      const data: ThroughputDataPoint[] = [
        { timestamp: '14:30:00', recordsPerSecond: 0, recordsPerMinute: 0, cumulativeRecords: 0 },
      ];
      render(<ThroughputChart data={data} />);
      // 0 < 1000 → Math.round(0).toString() = "0"
      const formatted = screen.getAllByText('0');
      expect(formatted.length).toBeGreaterThan(0);
    });
  });

  // =========================================================================
  // Summary stats row
  // =========================================================================
  describe('summary stats row', () => {
    it('displays current throughput value (latest data point)', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      // Last recordsPerSecond value = 17500 → formatThroughputValue(17500) = "17.5K"
      expect(screen.getByText('17.5K')).toBeInTheDocument();
    });

    it('displays peak throughput value (maximum in dataset)', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      // Max recordsPerSecond = 18200 → formatThroughputValue(18200) = "18.2K"
      expect(screen.getByText('18.2K')).toBeInTheDocument();
    });

    it('displays average throughput value', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      // Average recordsPerSecond = 141400/10 = 14140 → formatThroughputValue(14140) = "14.1K"
      expect(screen.getByText('14.1K')).toBeInTheDocument();
    });

    it('renders three stat cards for current, peak, and average', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.getByText(/Current/i)).toBeInTheDocument();
      expect(screen.getByText(/Peak/i)).toBeInTheDocument();
      expect(screen.getByText(/Average/i)).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Loading state
  // =========================================================================
  describe('loading state', () => {
    it('renders loading skeleton when isLoading is true', () => {
      const { container } = render(
        <ThroughputChart data={mockThroughputData} isLoading={true} />,
      );
      // Chart should NOT be rendered while loading
      expect(screen.queryByTestId('area-chart')).not.toBeInTheDocument();
      // Skeleton pulse animation element should be present
      const pulseElements = container.querySelectorAll('.animate-pulse');
      expect(pulseElements.length).toBeGreaterThan(0);
    });

    it('renders chart when isLoading is false', () => {
      render(<ThroughputChart data={mockThroughputData} isLoading={false} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Empty state
  // =========================================================================
  describe('empty state', () => {
    it('renders empty state message when data array is empty', () => {
      render(<ThroughputChart data={emptyData} />);
      expect(screen.getByText(/No throughput data available/i)).toBeInTheDocument();
      expect(screen.queryByTestId('area-chart')).not.toBeInTheDocument();
    });

    it('does not render summary stats when data is empty', () => {
      render(<ThroughputChart data={emptyData} />);
      expect(screen.queryByText(/Current/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/Peak/i)).not.toBeInTheDocument();
    });
  });

  // =========================================================================
  // Color theme variants
  // =========================================================================
  describe('color theme variants', () => {
    it('applies blue theme by default', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      const area = screen.getByTestId('area');
      // Blue theme primary color: #3B82F6
      expect(area).toHaveAttribute('data-stroke', '#3B82F6');
      expect(area).toHaveAttribute('data-fill', '#3B82F620');
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
    });

    it('applies green theme when colorTheme="green"', () => {
      render(<ThroughputChart data={mockThroughputData} colorTheme="green" />);
      const area = screen.getByTestId('area');
      // Green theme primary color: #10B981
      expect(area).toHaveAttribute('data-stroke', '#10B981');
      expect(area).toHaveAttribute('data-fill', '#10B98120');
    });

    it('applies purple theme when colorTheme="purple"', () => {
      render(<ThroughputChart data={mockThroughputData} colorTheme="purple" />);
      const area = screen.getByTestId('area');
      // Purple theme primary color: #8B5CF6
      expect(area).toHaveAttribute('data-stroke', '#8B5CF6');
      expect(area).toHaveAttribute('data-fill', '#8B5CF620');
    });
  });

  // =========================================================================
  // Title display
  // =========================================================================
  describe('title display', () => {
    it('displays title when title prop is provided', () => {
      render(<ThroughputChart data={mockThroughputData} title="Generation Throughput" />);
      expect(screen.getByText('Generation Throughput')).toBeInTheDocument();
    });

    it('does not render title element when title prop is not provided', () => {
      render(<ThroughputChart data={mockThroughputData} />);
      expect(screen.queryByText('Generation Throughput')).not.toBeInTheDocument();
    });
  });

  // =========================================================================
  // Custom height and className
  // =========================================================================
  describe('custom height and className', () => {
    it('passes custom height to chart', () => {
      render(<ThroughputChart data={mockThroughputData} height={500} />);
      const container = screen.getByTestId('responsive-container');
      expect(container).toHaveAttribute('data-height', '500');
    });

    it('applies custom className to container', () => {
      const { container } = render(
        <ThroughputChart data={mockThroughputData} className="custom-chart" />,
      );
      expect(container.firstChild).toHaveClass('custom-chart');
    });
  });
});
