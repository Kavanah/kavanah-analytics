import { type Page, type Locator } from '@playwright/test';
import { BasePage } from './base.page';

export class DashboardBuilderPage extends BasePage {
  readonly canvas: Locator;
  readonly canvasEmptyState: Locator;
  readonly chartGrid: Locator;
  readonly titleInput: Locator;
  readonly dashboardSelect: Locator;
  readonly saveButton: Locator;
  readonly newButton: Locator;
  readonly configPanel: Locator;
  readonly configTitle: Locator;
  readonly configType: Locator;
  readonly timePreset: Locator;
  readonly showHiddenCheckbox: Locator;
  readonly explorer: Locator;

  constructor(page: Page) {
    super(page);
    this.canvas = page.locator('#db-canvas');
    this.canvasEmptyState = page.locator('#canvas-empty-state');
    this.chartGrid = page.locator('#chart-grid');
    this.titleInput = page.locator('#dash-title');
    this.dashboardSelect = page.locator('#dash-select');
    this.saveButton = page.locator('#btn-save');
    this.newButton = page.locator('.db-topbar .db-btn-add').first();
    this.configPanel = page.locator('#config-panel');
    this.configTitle = page.locator('#cfg-title');
    this.configType = page.locator('#cfg-type');
    this.timePreset = page.locator('#builder-time-preset');
    this.showHiddenCheckbox = page.locator('#db-show-hidden-checkbox');
    this.explorer = page.locator('#dashboard-explorer');
  }

  /**
   * Locate a rendered explorer item by its dashboard title. The label
   * lives in a `.db-explorer-item-label` span; hidden dashboards carry
   * the `is-hidden` modifier on that span (builder.js renderDashboardExplorer).
   */
  explorerItem(title: string): Locator {
    return this.explorer.locator('.db-explorer-item-label', { hasText: title });
  }

  async goto() {
    await this.page.goto(this.prefix + '/dashboard-builder/');
    await this.canvas.waitFor();
  }

  /**
   * Click "+ New" and fill the dashboard name modal.
   * newDashboard() in builder.js shows an HTML modal (#new-dash-modal).
   */
  async clickNew(title: string = 'Untitled Dashboard') {
    await this.newButton.click();
    // Wait for the HTML modal to appear
    const modal = this.page.locator('#new-dash-modal');
    await modal.waitFor({ state: 'visible' });
    // Fill the name input and confirm
    const nameInput = this.page.locator('#new-dash-name-input');
    await nameInput.fill(title);
    await this.page.locator('#new-dash-confirm').click();
    // Wait for the modal to close and API call to complete
    await modal.waitFor({ state: 'hidden' });
    await this.page.waitForTimeout(500);
  }

  // Map chart type IDs to the visible label rendered in the modal tiles.
  // Source: bin/python/asset_management/web/templates/dashboard_builder/index.html
  private static readonly CHART_TYPE_LABELS: Record<string, string> = {
    bar: 'Bar Chart',
    line: 'Line Chart',
    pie: 'Pie Chart',
    table: 'Table',
    metric: 'Metric / KPI',
    grouped_table: 'Grouped Table',
  };

  getChartTypeTile(chartType: string): Locator {
    const label = DashboardBuilderPage.CHART_TYPE_LABELS[chartType];
    if (!label) {
      throw new Error(`Unknown chart type "${chartType}" — update CHART_TYPE_LABELS in dashboard-builder.page.ts`);
    }
    // Match the tile by its visible label inside the modal. The tiles do
    // not carry a data-chart-type attribute; they invoke
    // addChartToCanvas('<type>') via onclick.
    return this.page.locator('#add-chart-modal .chart-type-tile', { hasText: label });
  }

  /**
   * Add a chart to the canvas via the real UX:
   *   1. Click "+ Add Chart" — opens #add-chart-modal
   *   2. Click the tile for the requested chart type
   *   3. Modal closes; addChartToCanvas() inserts a slot into the grid
   *
   * The legacy name dragChartToCanvas is preserved so existing specs do
   * not need touching, but the implementation is click-based — there is
   * no drag-drop affordance in the current builder.
   */
  async dragChartToCanvas(chartType: string) {
    // Open the modal.
    await this.page.locator('.db-btn-add', { hasText: '+ Add Chart' }).first().click();
    const modal = this.page.locator('#add-chart-modal');
    await modal.waitFor({ state: 'visible' });
    // Click the tile.
    await this.getChartTypeTile(chartType).click();
    // Modal closes after addChartToCanvas() runs.
    await modal.waitFor({ state: 'hidden' });
  }

  async getCanvasChartCount(): Promise<number> {
    return this.chartGrid.locator('.chart-slot').count();
  }

  async selectChart(index: number) {
    await this.chartGrid.locator('.chart-slot').nth(index).click();
  }

  async configureChartTitle(title: string) {
    await this.configTitle.fill(title);
  }

  async saveDashboard() {
    await this.saveButton.click();
  }

  async loadDashboard(id: string) {
    await this.dashboardSelect.selectOption(id);
  }

  async getDashboardOptions(): Promise<string[]> {
    return this.dashboardSelect.locator('option').allTextContents();
  }
}
