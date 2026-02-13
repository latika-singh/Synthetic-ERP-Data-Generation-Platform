/**
 * QualityScoreGauge.test.tsx
 *
 * Comprehensive Vitest + React Testing Library unit tests for the
 * QualityScoreGauge Recharts radial bar chart component.
 *
 * Tests cover:
 *   - RadialBarChart gauge rendering with correct data binding
 *   - Overall score percentage display as centered numeric text
 *   - Color-coded thresholds (green ≥95%, amber 80-95%, orange 60-80%, red <60%)
 *   - Score breakdown panel with 3 weighted sub-scores
 *     (40% statistical fidelity, 30% business rules, 30% referential integrity)
 *     showing individual progress bars and weighted contribution calculations
 *   - Threshold target line at 95% default
 *   - Meets/below target indicator text
 *   - Size variant rendering for sm (150px), md (200px), and lg (280px)
 *   - Loading skeleton state with pulse animation
 *   - Title display and custom className propagation
 *
 * Mocks Recharts RadialBarChart components with functional component stubs
 * rendering testable DOM elements for assertion-based verification in jsdom.
 *
 * @see src/web/src/components/charts/QualityScoreGauge.tsx
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import QualityScoreGauge from '@/components/charts/QualityScoreGauge';
import type { QualityScoreBreakdown } from '@/components/charts/QualityScoreGauge';

// ---------------------------------------------------------------------------
// Recharts Mock Setup
// ---------------------------------------------------------------------------
// Mock the recharts module with functional component stubs that render
// testable DOM elements (divs with data-testid attributes) instead of
// actual SVG charts, enabling assertion-based verification in jsdom.
// Each mock renders a div with data-testid matching the component name
// for easy querying via screen.getByTestId().

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: any) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  RadialBarChart: ({ children }: any) => (
    <div data-testid="radial-bar-chart">{children}</div>
  ),
  RadialBar: ({ children, dataKey }: any) => (
    <div data-testid="radial-bar" data-datakey={dataKey}>
      {children}
    </div>
  ),
  PolarAngleAxis: () => <div data-testid="polar-angle-axis" />,
  Cell: () => <div data-testid="cell" />,
  Legend: () => <div data-testid="legend" />,
  Tooltip: () => <div data-testid="tooltip" />,
}));

// ---------------------------------------------------------------------------
// Test Data Fixtures
// ---------------------------------------------------------------------------

/**
 * High score breakdown — all sub-scores above 95% (green threshold).
 * Overall weighted score:
 *   0.97 × 0.4 + 0.96 × 0.3 + 0.98 × 0.3
 *   = 0.388 + 0.288 + 0.294
 *   = 0.97 (97%)
 */
const highScoreBreakdown: QualityScoreBreakdown = {
  statistical: 0.97,
  businessRules: 0.96,
  referentialIntegrity: 0.98,
};

/**
 * Medium score breakdown — amber range (80-95%).
 * Overall weighted score:
 *   0.88 × 0.4 + 0.85 × 0.3 + 0.90 × 0.3
 *   = 0.352 + 0.255 + 0.270
 *   = 0.877 (88%)
 */
const mediumScoreBreakdown: QualityScoreBreakdown = {
  statistical: 0.88,
  businessRules: 0.85,
  referentialIntegrity: 0.90,
};

/**
 * Low score breakdown — orange/red range (<80%).
 * Overall weighted score:
 *   0.70 × 0.4 + 0.65 × 0.3 + 0.72 × 0.3
 *   = 0.280 + 0.195 + 0.216
 *   = 0.691 (69%)
 */
const lowScoreBreakdown: QualityScoreBreakdown = {
  statistical: 0.70,
  businessRules: 0.65,
  referentialIntegrity: 0.72,
};

// ---------------------------------------------------------------------------
// Test Suites
// ---------------------------------------------------------------------------

describe('QualityScoreGauge', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // =========================================================================
  // RadialBarChart gauge rendering
  // =========================================================================
  describe('RadialBarChart gauge rendering', () => {
    it('renders RadialBarChart with correct gauge structure', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
        />,
      );
      expect(screen.getByTestId('radial-bar-chart')).toBeInTheDocument();
      expect(screen.getByTestId('radial-bar')).toBeInTheDocument();
    });

    it('renders ResponsiveContainer wrapper', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(screen.getByTestId('responsive-container')).toBeInTheDocument();
    });

    it('renders PolarAngleAxis for domain setup', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(screen.getByTestId('polar-angle-axis')).toBeInTheDocument();
    });

    it('passes correct dataKey to RadialBar', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(screen.getByTestId('radial-bar')).toHaveAttribute(
        'data-datakey',
        'value',
      );
    });
  });

  // =========================================================================
  // Overall score percentage display
  // =========================================================================
  describe('overall score percentage display', () => {
    it('displays overall score as percentage text (97%)', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(screen.getByText('97%')).toBeInTheDocument();
    });

    it('displays 88% for medium score', () => {
      render(<QualityScoreGauge overallScore={0.877} />);
      expect(screen.getByText('88%')).toBeInTheDocument();
    });

    it('displays 0% for zero score', () => {
      render(<QualityScoreGauge overallScore={0} />);
      expect(screen.getByText('0%')).toBeInTheDocument();
    });

    it('displays 100% for perfect score', () => {
      render(<QualityScoreGauge overallScore={1.0} />);
      expect(screen.getByText('100%')).toBeInTheDocument();
    });

    it('displays "Quality Score" label text beneath percentage', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(screen.getByText('Quality Score')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Color-coded thresholds
  // =========================================================================
  describe('color-coded thresholds', () => {
    it('uses green color (#10B981) for score ≥95%', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      const scoreEl = screen.getByText('97%');
      expect(scoreEl).toHaveStyle({ color: '#10B981' });
    });

    it('uses green color for exactly 95%', () => {
      render(<QualityScoreGauge overallScore={0.95} />);
      const scoreEl = screen.getByText('95%');
      expect(scoreEl).toHaveStyle({ color: '#10B981' });
    });

    it('uses amber color (#F59E0B) for score 80-94%', () => {
      render(<QualityScoreGauge overallScore={0.877} />);
      const scoreEl = screen.getByText('88%');
      expect(scoreEl).toHaveStyle({ color: '#F59E0B' });
    });

    it('uses red color (#EF4444) for score <60%', () => {
      render(<QualityScoreGauge overallScore={0.5} />);
      const scoreEl = screen.getByText('50%');
      expect(scoreEl).toHaveStyle({ color: '#EF4444' });
    });

    it('uses orange color (#F97316) for score 60-79%', () => {
      render(<QualityScoreGauge overallScore={0.691} />);
      const scoreEl = screen.getByText('69%');
      expect(scoreEl).toHaveStyle({ color: '#F97316' });
    });
  });

  // =========================================================================
  // Score breakdown panel with weighted sub-scores
  // =========================================================================
  describe('score breakdown panel with weighted sub-scores', () => {
    it('displays Statistical Fidelity label with 40% weight', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={true}
        />,
      );
      expect(screen.getByText('Statistical Fidelity')).toBeInTheDocument();
      expect(screen.getByText(/×40%/)).toBeInTheDocument();
    });

    it('displays Business Rules label with 30% weight', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={true}
        />,
      );
      expect(screen.getByText('Business Rules')).toBeInTheDocument();
      // Both Business Rules and Referential Integrity share 30% weight
      const weight30Elements = screen.getAllByText(/×30%/);
      expect(weight30Elements.length).toBeGreaterThanOrEqual(1);
    });

    it('displays Referential Integrity label with 30% weight', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={true}
        />,
      );
      expect(screen.getByText('Referential Integrity')).toBeInTheDocument();
    });

    it('shows individual sub-score percentages', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={true}
        />,
      );
      // Overall score (97%) and statistical sub-score (97%) both render "97%"
      // so we use getAllByText to handle the expected multiple matches
      const elements97 = screen.getAllByText(/97%/);
      expect(elements97.length).toBeGreaterThanOrEqual(1);
      // Business rules sub-score: 96%
      expect(screen.getByText(/96%/)).toBeInTheDocument();
      // Referential integrity sub-score: 98%
      expect(screen.getByText(/98%/)).toBeInTheDocument();
    });

    it('shows weighted contribution calculations', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={true}
        />,
      );
      // statistical:  97% × 40% = 39%
      // businessRules: 96% × 30% = 29%
      // referentialIntegrity: 98% × 30% = 29%
      expect(screen.getByText(/= 39%/)).toBeInTheDocument();
    });

    it('renders "Score Breakdown" heading', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={true}
        />,
      );
      expect(screen.getByText('Score Breakdown')).toBeInTheDocument();
    });

    it('hides breakdown when showBreakdown is false', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={false}
        />,
      );
      expect(screen.queryByText('Score Breakdown')).not.toBeInTheDocument();
      expect(
        screen.queryByText('Statistical Fidelity'),
      ).not.toBeInTheDocument();
    });

    it('hides breakdown when breakdown prop is not provided', () => {
      render(
        <QualityScoreGauge overallScore={0.97} showBreakdown={true} />,
      );
      expect(screen.queryByText('Score Breakdown')).not.toBeInTheDocument();
    });

    it('shows low score breakdown values correctly', () => {
      render(
        <QualityScoreGauge
          overallScore={0.691}
          breakdown={lowScoreBreakdown}
          showBreakdown={true}
        />,
      );
      expect(screen.getByText('Score Breakdown')).toBeInTheDocument();
      // low sub-score: 70% statistical, 65% business rules, 72% referential integrity
      expect(screen.getByText(/70%/)).toBeInTheDocument();
      expect(screen.getByText(/65%/)).toBeInTheDocument();
      expect(screen.getByText(/72%/)).toBeInTheDocument();
    });

    it('shows medium score breakdown values correctly', () => {
      render(
        <QualityScoreGauge
          overallScore={0.877}
          breakdown={mediumScoreBreakdown}
          showBreakdown={true}
        />,
      );
      expect(screen.getByText('Score Breakdown')).toBeInTheDocument();
      // medium sub-scores: 88% statistical, 85% business rules, 90% referential integrity
      const elements88 = screen.getAllByText(/88%/);
      expect(elements88.length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText(/85%/)).toBeInTheDocument();
      expect(screen.getByText(/90%/)).toBeInTheDocument();
    });

    it('renders progress bars for each sub-score', () => {
      const { container } = render(
        <QualityScoreGauge
          overallScore={0.97}
          breakdown={highScoreBreakdown}
          showBreakdown={true}
        />,
      );
      // Verify 3 progress bar elements are rendered (one per sub-score)
      const progressBars = container.querySelectorAll('[role="progressbar"]');
      expect(progressBars).toHaveLength(3);

      // Verify colored indicators match BREAKDOWN_COLORS constants
      // Breakdown dimension order from Object.entries(BREAKDOWN_LABELS):
      //   statistical:          #3B82F6 (blue-500)
      //   businessRules:        #8B5CF6 (violet-500)
      //   referentialIntegrity: #06B6D4 (cyan-500)
      const expectedColors = ['#3B82F6', '#8B5CF6', '#06B6D4'];
      progressBars.forEach((bar, index) => {
        expect(bar).toHaveStyle({
          backgroundColor: expectedColors[index],
        });
      });
    });
  });

  // =========================================================================
  // Threshold target line
  // =========================================================================
  describe('threshold target line', () => {
    it('displays default threshold of 95%', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(screen.getByText(/Target: ≥95%/)).toBeInTheDocument();
    });

    it('displays custom threshold when threshold prop provided', () => {
      render(
        <QualityScoreGauge overallScore={0.97} threshold={0.9} />,
      );
      expect(screen.getByText(/Target: ≥90%/)).toBeInTheDocument();
    });

    it('displays weight distribution description', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(screen.getByText(/40% Stat/)).toBeInTheDocument();
      expect(screen.getByText(/30% Rules/)).toBeInTheDocument();
      expect(screen.getByText(/30% Integrity/)).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Meets/below target indicator
  // =========================================================================
  describe('meets/below target indicator', () => {
    it('displays "Meets Target" with checkmark when score ≥ threshold', () => {
      render(
        <QualityScoreGauge overallScore={0.97} threshold={0.95} />,
      );
      expect(screen.getByText(/Meets Target/i)).toBeInTheDocument();
      expect(screen.getByText(/✓/)).toBeInTheDocument();
    });

    it('displays "Below Target" with warning when score < threshold', () => {
      render(
        <QualityScoreGauge overallScore={0.877} threshold={0.95} />,
      );
      expect(screen.getByText(/Below Target/i)).toBeInTheDocument();
      expect(screen.getByText(/⚠/)).toBeInTheDocument();
    });

    it('shows threshold percentage in "Below Target" message', () => {
      render(
        <QualityScoreGauge overallScore={0.877} threshold={0.95} />,
      );
      // Verify the Below Target message specifically contains the threshold %
      const belowTargetEl = screen.getByText(/Below Target/i);
      expect(belowTargetEl).toHaveTextContent('95%');
    });

    it('displays "Meets Target" when score exactly equals threshold', () => {
      render(
        <QualityScoreGauge overallScore={0.95} threshold={0.95} />,
      );
      expect(screen.getByText(/Meets Target/i)).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Size variants
  // =========================================================================
  describe('size variants', () => {
    it('renders with sm size (150px)', () => {
      const { container } = render(
        <QualityScoreGauge overallScore={0.97} size="sm" />,
      );
      // The gauge chart wrapper div has explicit width/height inline styles
      const chartWrapper = container.querySelector(
        '.relative.flex.justify-center',
      );
      expect(chartWrapper).toHaveStyle({ width: '150px', height: '150px' });
    });

    it('renders with md size (200px) as default', () => {
      const { container } = render(
        <QualityScoreGauge overallScore={0.97} />,
      );
      // Default size is 'md' = 200px
      const chartWrapper = container.querySelector(
        '.relative.flex.justify-center',
      );
      expect(chartWrapper).toHaveStyle({ width: '200px', height: '200px' });
    });

    it('renders with lg size (280px)', () => {
      const { container } = render(
        <QualityScoreGauge overallScore={0.97} size="lg" />,
      );
      const chartWrapper = container.querySelector(
        '.relative.flex.justify-center',
      );
      expect(chartWrapper).toHaveStyle({ width: '280px', height: '280px' });
    });

    it('renders gauge chart successfully in each size variant', () => {
      const sizes: Array<'sm' | 'md' | 'lg'> = ['sm', 'md', 'lg'];
      sizes.forEach((size) => {
        const { unmount } = render(
          <QualityScoreGauge overallScore={0.97} size={size} />,
        );
        expect(screen.getByTestId('radial-bar-chart')).toBeInTheDocument();
        unmount();
      });
    });
  });

  // =========================================================================
  // Loading state
  // =========================================================================
  describe('loading state', () => {
    it('renders loading skeleton when isLoading is true', () => {
      render(
        <QualityScoreGauge overallScore={0.97} isLoading={true} />,
      );
      expect(
        screen.queryByTestId('radial-bar-chart'),
      ).not.toBeInTheDocument();
      expect(screen.queryByText('97%')).not.toBeInTheDocument();
    });

    it('does not render loading skeleton when isLoading is false', () => {
      render(
        <QualityScoreGauge overallScore={0.97} isLoading={false} />,
      );
      expect(screen.getByTestId('radial-bar-chart')).toBeInTheDocument();
      expect(screen.getByText('97%')).toBeInTheDocument();
    });
  });

  // =========================================================================
  // Title display
  // =========================================================================
  describe('title display', () => {
    it('displays title when title prop is provided', () => {
      render(
        <QualityScoreGauge
          overallScore={0.97}
          title="Job Quality Score"
        />,
      );
      expect(screen.getByText('Job Quality Score')).toBeInTheDocument();
    });

    it('does not render title element when title prop is not provided', () => {
      render(<QualityScoreGauge overallScore={0.97} />);
      expect(
        screen.queryByText('Job Quality Score'),
      ).not.toBeInTheDocument();
    });
  });

  // =========================================================================
  // Custom className
  // =========================================================================
  describe('custom className', () => {
    it('applies custom className to container', () => {
      const { container } = render(
        <QualityScoreGauge
          overallScore={0.97}
          className="my-custom-class"
        />,
      );
      expect(container.firstChild).toHaveClass('my-custom-class');
    });
  });
});
