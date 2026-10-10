import { type Page, type Locator } from '@playwright/test';
import { BasePage } from './base.page';

export class ImportDataPage extends BasePage {
  readonly statusSection: Locator;
  readonly csvSection: Locator;
  readonly confirmDialog: Locator;
  readonly confirmButton: Locator;
  readonly dataViewSelect: Locator;
  readonly runImportButton: Locator;
  readonly backfillFromDate: Locator;
  readonly backfillToDate: Locator;

  constructor(page: Page) {
    super(page);
    this.statusSection = page.locator('#section-status');
    this.csvSection = page.locator('#section-csv');
    this.confirmDialog = page.locator('#status-run-import-dialog');
    this.confirmButton = page.locator('#status-dialog-confirm-btn');
    this.dataViewSelect = page.locator('#status-data-view');
    this.runImportButton = page.locator('#backfill-all-btn');
    this.backfillFromDate = page.locator('#backfill-from-date');
    this.backfillToDate = page.locator('#backfill-to-date');
  }

  async goto() {
    await this.page.goto(this.prefix + '/import-data');
    await this.page.waitForLoadState('domcontentloaded');
  }
}
