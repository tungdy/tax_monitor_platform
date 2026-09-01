import { expect, test as base } from "@playwright/test";

import { fullValidationReport } from "./full-validation.fixture";

export const test = base.extend({
  page: async ({ page }, use) => {
    await page.route("**/api/v1/auth/session", async (route) => {
      await route.fulfill({
        json: {
          authenticated: true,
          subject: "e2e-reviewer",
          display_name: "端到端测试用户",
          avatar_url: null,
          auth_method: "test",
          roles: ["group-tax"],
          organization_path: "/group",
        },
      });
    });
    await page.route("**/api/v1/auth/config", async (route) => {
      await route.fulfill({
        json: { password_enabled: true, feishu_enabled: false },
      });
    });
    await page.route("**/real-validation-latest.json?*", async (route) => {
      await route.fulfill({ json: fullValidationReport });
    });
    await use(page);
  },
});

export { expect };
