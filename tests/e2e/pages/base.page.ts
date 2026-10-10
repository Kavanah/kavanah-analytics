import { type Page } from '@playwright/test';

/**
 * Returns the app prefix for asset-management routes.
 * On staging (behind nginx), DEAM routes live under /assets/.
 * Set APP_PREFIX=/assets to enable this.
 */
export function appPrefix(): string {
  return process.env.APP_PREFIX || '';
}

export class BasePage {
  protected prefix: string;

  constructor(protected page: Page) {
    this.prefix = appPrefix();
  }

  async navigateTo(path: string) {
    await this.page.goto(this.prefix + path);
  }

  async logout() {
    await this.page.goto(this.prefix + '/logout');
  }

  async getHeaderUsername(): Promise<string> {
    const el = this.page.locator('.header-user span').first();
    return (await el.textContent()) ?? '';
  }

  async isAuthenticated(): Promise<boolean> {
    // If visiting a protected page doesn't redirect to login, we're authenticated
    const url = this.page.url();
    return !url.includes('/login');
  }
}
