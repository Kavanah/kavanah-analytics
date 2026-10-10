import * as fs from 'node:fs';
import * as path from 'node:path';
import type {
  Reporter,
  TestCase,
  TestResult,
  FullResult,
} from '@playwright/test/reporter';

/**
 * Custom Playwright reporter that emits the same
 * release-plan JSON shape used by the pytest hook in `tests/conftest.py`
 *:
 *
 *   {
 *     generated_at: "2026-04-28T...Z",
 * release_plan: "plan-25",
 *     results: {
 *       "SEC-CSP-005": {
 *         outcome: "passed" | "failed" | "skipped",
 *         duration: 0.42,
 *         file: "...",
 *         node: "<describe> > <test>",
 *         source: "playwright",
 *         longrepr: null | "<error text>"
 *       }
 *     }
 *   }
 *
 * Tests opt in by attaching annotations:
 *   test.info().annotations.push({ type: 'test_id', description: 'SEC-CSP-005' });
 * test.info.annotations.push({ type: 'release_plan', description: 'plan-25' });
 *
 * Activation: env vars (same as pytest hook).
 *   RELEASE_PLAN_REPORT=/path/to/file.json
 * RELEASE_PLAN_NAME= (optional; filters to that plan if set)
 */

interface ReleasePlanEntry {
  outcome: 'passed' | 'failed' | 'skipped';
  duration: number;
  file: string;
  node: string;
  source: 'playwright';
  longrepr: string | null;
}

const ANNOTATION_TEST_ID = 'test_id';
const ANNOTATION_RELEASE_PLAN = 'release_plan';

function annotationValues(test: TestCase, type: string): string[] {
  return test.annotations.filter((a) => a.type === type).map((a) => a.description ?? '').filter(Boolean);
}

function mapOutcome(result: TestResult): 'passed' | 'failed' | 'skipped' {
  if (result.status === 'passed') return 'passed';
  if (result.status === 'skipped') return 'skipped';
  return 'failed';
}

function failureText(result: TestResult): string | null {
  if (result.status === 'passed' || result.status === 'skipped') return null;
  const errors = result.errors.map((e) => e.message ?? e.stack ?? '').filter(Boolean);
  if (errors.length) return errors.join('\n\n');
  return result.error?.message ?? result.error?.stack ?? 'Test failed without error message';
}

export default class ReleasePlanReporter implements Reporter {
  private results: Record<string, ReleasePlanEntry> = {};
  private targetPath = process.env.RELEASE_PLAN_REPORT ?? '';
  private planFilter = process.env.RELEASE_PLAN_NAME ?? '';

  onTestEnd(test: TestCase, result: TestResult): void {
    if (!this.targetPath) return;

    const testIds = annotationValues(test, ANNOTATION_TEST_ID);
    if (testIds.length === 0) return;

    if (this.planFilter) {
      const plans = annotationValues(test, ANNOTATION_RELEASE_PLAN);
      if (!plans.includes(this.planFilter)) return;
    }

    const testId = testIds[0];
    this.results[testId] = {
      outcome: mapOutcome(result),
      duration: Math.round(result.duration) / 1000,
      file: test.location.file,
      node: test.titlePath().slice(1).join(' > '),
      source: 'playwright',
      longrepr: failureText(result),
    };
  }

  async onEnd(_result: FullResult): Promise<void> {
    if (!this.targetPath) return;

    const payload = {
      generated_at: new Date().toISOString(),
      release_plan: this.planFilter || null,
      results: this.results,
    };

    const dir = path.dirname(this.targetPath);
    if (dir) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(this.targetPath, JSON.stringify(payload, null, 2) + '\n');
  }
}
