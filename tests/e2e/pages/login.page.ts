import { type Page, type Locator } from '@playwright/test';
import { BasePage } from './base.page';

export class LoginPage extends BasePage {
  readonly usernameInput: Locator;
  readonly passwordInput: Locator;
  readonly csrfInput: Locator;
  readonly submitButton: Locator;
  readonly errorAlert: Locator;
  readonly successAlert: Locator;

  constructor(page: Page) {
    super(page);
    this.usernameInput = page.locator('input[name="username"]');
    this.passwordInput = page.locator('input[name="password"]');
    this.csrfInput = page.locator('input[name="csrf_token"]');
    this.submitButton = page.locator('button[type="submit"]');
    this.errorAlert = page.locator('.login-alert-error');
    this.successAlert = page.locator('.login-alert-success');
  }

  async goto(context: 'assets' | 'dashboards' = 'assets') {
    const path = context === 'dashboards'
      ? '/dashboards/login'
      : this.prefix + '/login';
    await this.page.goto(path);
    await this.usernameInput.waitFor();
  }

  async fillCredentials(username: string, password: string) {
    await this.usernameInput.fill(username);
    await this.passwordInput.fill(password);
  }

  async submit() {
    await this.submitButton.click();
  }

  async getCsrfToken(): Promise<string> {
    return (await this.csrfInput.getAttribute('value')) ?? '';
  }

  async getErrorMessage(): Promise<string> {
    return (await this.errorAlert.textContent()) ?? '';
  }

  async getSuccessMessage(): Promise<string> {
    return (await this.successAlert.textContent()) ?? '';
  }
}
