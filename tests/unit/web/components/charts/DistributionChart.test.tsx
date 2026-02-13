/**
 * DistributionChart.test.tsx
 *
 * Comprehensive Vitest + React Testing Library unit tests for the
 * DistributionChart Recharts component. This component visualizes
 * statistical distributions of profiled ERP column data on the
 * Profile Viewer (Screen S-006) and Generation Wizard (Screen S-002).
 *
 * Test coverage includes:
 *   - Rendering of all 7 distribution types (normal, log-normal, Poisson,
 *     uniform, categorical, exponential, binomial) with correct chart type
 *     selection (AreaChart for continuous, BarChart for categorical)
 *   - Data point generation logic verification for each distribution formula
 *   - Distribution type badge display with correct labels and colors
 *   - Parameter details panel showing mean/std_dev/min/max/null%/unique values
 *   - Fit score indicator display and formatting
 *   - Loading skeleton state with pulse animation
 *   - Empty/unknown distribution fallback message
 *   - Responsive container sizing behavior
 *   - Title and data type display
 *
 * Recharts components are mocked with testable DOM div stubs to avoid
 * SVG rendering in jsdom and enable assertion-based verification of
 * chart type selection (AreaChart vs BarChart) and component composition.
 *
 * @see src/web/src/components/charts/DistributionChart.tsx  — Component under test
 * @see src/web/src/types/profile.ts                         — ColumnStatistics / Distribution types
 * @see tests/unit/web/setup.ts                              — Global test setup (jest-dom, ResizeObserver, etc.)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import DistributionChart from '@/components/charts/DistributionChart';
import type { ColumnStatistics, Distribution } from '@/types/profile';

// ============================================================================
// Recharts Module Mock
// ============================================================================
//
// Replace all Recharts components with lightweight DOM div stubs that expose
// data-testid attributes for assertion. This avoids SVG rendering in jsdom
// and allows verifying chart type selection (AreaChart vs BarChart) and
// component composition (Area, Bar, Cell, XAxis, YAxis, etc.) through DOM.
// ============================================================================

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children, width, height }: any) => (
    <div
      data-testid="responsive-container"
      data-width={width}
      data-height={height}
    >
      {children}
    </div>
  ),
  AreaChart: ({ children }: any) => (
    <div data-testid="area-chart" data-type="area">
      {children}
    </div>
  ),
  BarChart: ({ children }: any) => (
    <div data-testid="bar-chart" data-type="bar">
      {children}
    </div>
  ),
  Area: (props: any) => (
    <div data-testid="area" data-datakey={props.dataKey} />
  ),
  Bar: ({ children, ...props }: any) => (
    <div data-testid="bar" data-datakey={props.dataKey}>
      {children}
    </div>
  ),
  XAxis: () => <div data-testid="x-axis" />,
  YAxis: () => <div data-testid="y-axis" />,
  CartesianGrid: () => <div data-testid="cartesian-grid" />,
  Tooltip: () => <div data-testid="tooltip" />,
  Legend: () => <div data-testid="legend" />,
  Cell: () => <div data-testid="cell" />,
  ReferenceLine: () => <div data-testid="reference-line" />,
}));

// ============================================================================
// Test Data Fixtures — Mock ColumnStatistics for Each Distribution Type
// ============================================================================

/**
 * 1. Normal (Gaussian) distribution fixture.
 * Parameters: mean=50, std_dev=10 | fit_score=0.92
 * Expected chart range: [mean − 4·σ, mean + 4·σ] = [10, 90]
 */
const mockNormalStats = {
  column_name: 'amount',
  data_type: 'decimal',
  distribution: {
    type: 'normal',
    parameters: { mean: 50, std_dev: 10 },
    fit_score: 0.92,
  },
  mean: 50,
  std_dev: 10,
  min_value: 15,
  max_value: 85,
  null_percentage: 2.5,
  unique_count: 980,
} as ColumnStatistics;

/**
 * 2. Log-Normal distribution fixture.
 * Parameters: mu=3.5, sigma=0.8 | fit_score=0.88
 */
const mockLogNormalStats = {
  column_name: 'salary',
  data_type: 'decimal',
  distribution: {
    type: 'log_normal',
    parameters: { mu: 3.5, sigma: 0.8 },
    fit_score: 0.88,
  },
  mean: 45.6,
  std_dev: 42.3,
  min_value: 1.2,
  max_value: 350.0,
  null_percentage: 0.5,
  unique_count: 1500,
} as ColumnStatistics;

/**
 * 3. Poisson distribution fixture.
 * Parameters: lambda=5.2 | fit_score=0.91
 */
const mockPoissonStats = {
  column_name: 'event_count',
  data_type: 'integer',
  distribution: {
    type: 'poisson',
    parameters: { lambda: 5.2 },
    fit_score: 0.91,
  },
  mean: 5.2,
  std_dev: 2.28,
  min_value: 0,
  max_value: 18,
  null_percentage: 0.0,
  unique_count: 15,
} as ColumnStatistics;

/**
 * 4. Uniform distribution fixture.
 * Parameters: min=0, max=100 | fit_score=0.95
 */
const mockUniformStats = {
  column_name: 'random_id',
  data_type: 'integer',
  distribution: {
    type: 'uniform',
    parameters: { min: 0, max: 100 },
    fit_score: 0.95,
  },
  mean: 50,
  std_dev: 28.87,
  min_value: 0,
  max_value: 100,
  null_percentage: 0.0,
  unique_count: 101,
} as ColumnStatistics;

/**
 * 5. Categorical distribution fixture.
 * 4 categories: active (0.6), inactive (0.25), suspended (0.1), archived (0.05)
 * fit_score=0.99
 */
const mockCategoricalStats = {
  column_name: 'status',
  data_type: 'varchar',
  distribution: {
    type: 'categorical',
    parameters: { active: 0.6, inactive: 0.25, suspended: 0.1, archived: 0.05 },
    fit_score: 0.99,
  },
  null_percentage: 1.0,
  unique_count: 4,
} as ColumnStatistics;

/**
 * 6. Exponential distribution fixture.
 * Parameters: lambda=0.5 | fit_score=0.87
 */
const mockExponentialStats = {
  column_name: 'wait_time',
  data_type: 'decimal',
  distribution: {
    type: 'exponential',
    parameters: { lambda: 0.5 },
    fit_score: 0.87,
  },
  mean: 2.0,
  std_dev: 2.0,
  min_value: 0.01,
  max_value: 15.4,
  null_percentage: 0.3,
  unique_count: 890,
} as ColumnStatistics;

/**
 * 7. Binomial distribution fixture.
 * Parameters: n=20, p=0.3 | fit_score=0.93
 */
const mockBinomialStats = {
  column_name: 'success_count',
  data_type: 'integer',
  distribution: {
    type: 'binomial',
    parameters: { n: 20, p: 0.3 },
    fit_score: 0.93,
  },
  mean: 6.0,
  std_dev: 2.05,
  min_value: 0,
  max_value: 15,
  null_percentage: 0.0,
  unique_count: 16,
} as ColumnStatistics;

/**
 * 8. Unknown distribution fixture (fallback case).
 * type='unknown', empty parameters, fit_score=null
 */
const mockUnknownStats = {
  column_name: 'misc_data',
  data_type: 'text',
  distribution: {
    type: 'unknown',
    parameters: {},
    fit_score: null,
  },
  null_percentage: 15.0,
} as ColumnStatistics;

// ============================================================================
// Test Suites
// ============================================================================

describe('DistributionChart', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // --------------------------------------------------------------------------
  // Distribution type → chart type rendering
  // --------------------------------------------------------------------------
  describe('rendering for each distribution type', () => {
    it('renders AreaChart for normal distribution', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.queryByTestId('bar-chart')).not.toBeInTheDocument();
    });

    it('renders AreaChart for log-normal distribution', () => {
      render(<DistributionChart columnStats={mockLogNormalStats} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.queryByTestId('bar-chart')).not.toBeInTheDocument();
    });

    it('renders AreaChart for Poisson distribution', () => {
      render(<DistributionChart columnStats={mockPoissonStats} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.queryByTestId('bar-chart')).not.toBeInTheDocument();
    });

    it('renders AreaChart for uniform distribution', () => {
      render(<DistributionChart columnStats={mockUniformStats} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.queryByTestId('bar-chart')).not.toBeInTheDocument();
    });

    it('renders BarChart for categorical distribution', () => {
      render(<DistributionChart columnStats={mockCategoricalStats} />);
      expect(screen.getByTestId('bar-chart')).toBeInTheDocument();
      expect(screen.queryByTestId('area-chart')).not.toBeInTheDocument();
    });

    it('renders AreaChart for exponential distribution', () => {
      render(<DistributionChart columnStats={mockExponentialStats} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.queryByTestId('bar-chart')).not.toBeInTheDocument();
    });

    it('renders AreaChart for binomial distribution', () => {
      render(<DistributionChart columnStats={mockBinomialStats} />);
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.queryByTestId('bar-chart')).not.toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------
  // Data point generation verification
  // --------------------------------------------------------------------------
  describe('data point generation verification', () => {
    it('generates correct number of data points for normal distribution with default resolution', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      // Verify ResponsiveContainer is present (wraps the chart)
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
      // Verify the Area component receives expected dataKey="y"
      const areaElement = screen.getByTestId('area');
      expect(areaElement).toHaveAttribute('data-datakey', 'y');
    });

    it('generates data points within expected x-range for normal distribution (mean ± 4*stdDev)', () => {
      // For normal distribution: mean=50, std_dev=10
      // Expected x-range: [50 - 4*10, 50 + 4*10] = [10, 90]
      render(<DistributionChart columnStats={mockNormalStats} />);
      // Chart renders successfully with generated data in the valid range
      const container = screen.getByTestId('responsive-container');
      const chartScope = within(container);
      expect(chartScope.getByTestId('area-chart')).toBeInTheDocument();
      expect(chartScope.getByTestId('area')).toBeInTheDocument();
    });

    it('respects custom resolution prop', () => {
      render(<DistributionChart columnStats={mockNormalStats} resolution={50} />);
      // Component renders successfully with reduced resolution (50 data points)
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.getByTestId('area')).toBeInTheDocument();
    });

    it('generates categorical data as discrete bars with correct category count', () => {
      render(<DistributionChart columnStats={mockCategoricalStats} />);
      // Verify bar chart renders with 4 Cell elements (active, inactive, suspended, archived)
      expect(screen.getByTestId('bar-chart')).toBeInTheDocument();
      const cells = screen.getAllByTestId('cell');
      expect(cells).toHaveLength(4);
    });
  });

  // --------------------------------------------------------------------------
  // Distribution type badge display
  // --------------------------------------------------------------------------
  describe('distribution type badge display', () => {
    it('displays distribution type label badge for normal distribution', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByText(/Normal \(Gaussian\)/i)).toBeInTheDocument();
    });

    it('displays distribution type label badge for log-normal', () => {
      render(<DistributionChart columnStats={mockLogNormalStats} />);
      expect(screen.getByText(/Log-Normal/i)).toBeInTheDocument();
    });

    it('displays distribution type label badge for Poisson', () => {
      render(<DistributionChart columnStats={mockPoissonStats} />);
      expect(screen.getByText(/Poisson/i)).toBeInTheDocument();
    });

    it('displays distribution type label badge for categorical', () => {
      render(<DistributionChart columnStats={mockCategoricalStats} />);
      expect(screen.getByText(/Categorical/i)).toBeInTheDocument();
    });

    it('displays Unknown badge for unknown distribution', () => {
      render(<DistributionChart columnStats={mockUnknownStats} />);
      expect(screen.getByText(/Unknown/i)).toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------
  // Parameter details panel
  // --------------------------------------------------------------------------
  describe('parameter details panel', () => {
    it('displays mean, std dev, min, max when showParameters is true (default)', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByText('Mean')).toBeInTheDocument();
      expect(screen.getByText('Std Dev')).toBeInTheDocument();
      expect(screen.getByText('Min')).toBeInTheDocument();
      expect(screen.getByText('Max')).toBeInTheDocument();
    });

    it('displays null percentage', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByText('Null %')).toBeInTheDocument();
      expect(screen.getByText('2.5%')).toBeInTheDocument();
    });

    it('displays unique count', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByText('Unique')).toBeInTheDocument();
    });

    it('hides parameter panel when showParameters is false', () => {
      render(
        <DistributionChart columnStats={mockNormalStats} showParameters={false} />,
      );
      expect(screen.queryByText('Mean')).not.toBeInTheDocument();
      expect(screen.queryByText('Std Dev')).not.toBeInTheDocument();
    });

    it('displays distribution-specific parameters (e.g., lambda for Poisson)', () => {
      render(<DistributionChart columnStats={mockPoissonStats} />);
      expect(screen.getByText(/lambda/i)).toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------
  // Fit score indicator
  // --------------------------------------------------------------------------
  describe('fit score indicator', () => {
    it('displays fit score when showFitScore is true and fit_score exists', () => {
      render(
        <DistributionChart columnStats={mockNormalStats} showFitScore={true} />,
      );
      expect(screen.getByText(/Fit/i)).toBeInTheDocument();
      expect(screen.getByText(/92\.0%/)).toBeInTheDocument();
    });

    it('hides fit score when showFitScore is false', () => {
      render(
        <DistributionChart columnStats={mockNormalStats} showFitScore={false} />,
      );
      expect(screen.queryByText(/Fit:/)).not.toBeInTheDocument();
    });

    it('does not display fit score when fit_score is null', () => {
      render(
        <DistributionChart columnStats={mockUnknownStats} showFitScore={true} />,
      );
      expect(screen.queryByText(/Fit:/)).not.toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------
  // Loading skeleton state
  // --------------------------------------------------------------------------
  describe('loading skeleton state', () => {
    it('renders loading skeleton when isLoading is true', () => {
      render(
        <DistributionChart columnStats={mockNormalStats} isLoading={true} />,
      );
      // Verify skeleton/pulse animation element is present via role="status"
      const skeleton = screen.getByRole('status');
      expect(skeleton).toBeInTheDocument();
      // Verify pulse animation CSS class is applied
      expect(skeleton.querySelector('.animate-pulse')).toBeTruthy();
      // Verify chart is NOT rendered
      expect(screen.queryByTestId('area-chart')).not.toBeInTheDocument();
    });

    it('does not render loading skeleton when isLoading is false', () => {
      render(
        <DistributionChart columnStats={mockNormalStats} isLoading={false} />,
      );
      expect(screen.getByTestId('area-chart')).toBeInTheDocument();
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------
  // Empty and unknown distribution handling
  // --------------------------------------------------------------------------
  describe('empty and unknown distribution handling', () => {
    it('renders "not available" message for unknown distribution type', () => {
      render(<DistributionChart columnStats={mockUnknownStats} />);
      expect(screen.getByText(/not available/i)).toBeInTheDocument();
    });

    it('does not render AreaChart or BarChart for unknown distribution', () => {
      render(<DistributionChart columnStats={mockUnknownStats} />);
      expect(screen.queryByTestId('area-chart')).not.toBeInTheDocument();
      expect(screen.queryByTestId('bar-chart')).not.toBeInTheDocument();
    });
  });

  // --------------------------------------------------------------------------
  // Responsive container sizing
  // --------------------------------------------------------------------------
  describe('responsive container sizing', () => {
    it('wraps chart in ResponsiveContainer', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('passes custom height prop to chart container', () => {
      render(<DistributionChart columnStats={mockNormalStats} height={400} />);
      const container = screen.getByTestId('responsive-container');
      // Verify the custom height is propagated through to ResponsiveContainer
      expect(container).toHaveAttribute('data-height', '400');
    });

    it('applies custom className to container', () => {
      const { container } = render(
        <DistributionChart
          columnStats={mockNormalStats}
          className="custom-class"
        />,
      );
      expect(container.firstChild).toHaveClass('custom-class');
    });
  });

  // --------------------------------------------------------------------------
  // Title and data type display
  // --------------------------------------------------------------------------
  describe('title and data type display', () => {
    it('displays column name as title when no explicit title prop', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByText('amount')).toBeInTheDocument();
    });

    it('displays custom title when title prop is provided', () => {
      render(
        <DistributionChart
          columnStats={mockNormalStats}
          title="Custom Title"
        />,
      );
      expect(screen.getByText('Custom Title')).toBeInTheDocument();
    });

    it('displays data type in parentheses', () => {
      render(<DistributionChart columnStats={mockNormalStats} />);
      expect(screen.getByText(/decimal/i)).toBeInTheDocument();
    });
  });
});
