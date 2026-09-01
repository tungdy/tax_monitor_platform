import type { Page } from "@playwright/test";

import { expect, test } from "./test-fixture";

const entryUrl = process.env.PLAYWRIGHT_ENTRY_URL ?? ".";

const refundResults = {
  refund_tax_year: 2025,
  scan_period: "2026-03",
  received_count: 4,
  not_received_count: 1,
  wrong_account_count: 2,
  ambiguous_count: 1,
  received: [
    {
      target_id: "10000000-0000-4000-8000-000000000001",
      company_id: "20000000-0000-4000-8000-000000000001",
      company_code: "3000",
      company_name: "已正确入账公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "120000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "CORRECT",
      account_family: "INCOME_TAX_EXPENSE",
      receipt_source: "SAP_MATCH",
      matched_amount: "120000.00",
      gl_account_code: "6801010000",
      gl_account_name: "所得税费用",
      document_number: "510001",
      line_item: "001",
      posting_date: "2026-03-08",
      alert_code: null,
      writeback_status: "SUCCEEDED",
    },
    {
      target_id: "10000000-0000-4000-8000-000000000002",
      company_id: "20000000-0000-4000-8000-000000000002",
      company_code: "3560",
      company_name: "错误科目公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "80000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "WRONG_ACCOUNT",
      account_family: "OTHER_INCOME",
      receipt_source: "SAP_MATCH",
      matched_amount: "80000.00",
      gl_account_code: "6117990000",
      gl_account_name: "其他收益",
      document_number: "510002",
      line_item: "002",
      posting_date: "2026-03-12",
      alert_code: "REFUND_BOOKED_TO_WRONG_ACCOUNT",
      writeback_status: "PENDING",
    },
    {
      target_id: "10000000-0000-4000-8000-000000000005",
      company_id: "20000000-0000-4000-8000-000000000005",
      company_code: "6000",
      company_name: "应交税费错误入账公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "60000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "WRONG_ACCOUNT",
      account_family: "TAXES_PAYABLE",
      receipt_source: "SAP_MATCH",
      matched_amount: "60000.00",
      gl_account_code: "2221130000",
      gl_account_name: "应交税费-企业所得税",
      document_number: "510003",
      line_item: "003",
      posting_date: "2026-03-15",
      alert_code: "REFUND_BOOKED_TO_WRONG_ACCOUNT",
      writeback_status: "PENDING",
    },
    {
      target_id: "10000000-0000-4000-8000-000000000006",
      company_id: "20000000-0000-4000-8000-000000000006",
      company_code: "7000",
      company_name: "飞书手工登记公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "40000.00",
      currency: "CNY",
      receipt_status: "RECEIVED",
      booking_status: "NOT_APPLICABLE",
      account_family: null,
      receipt_source: "LARK_MANUAL",
      matched_amount: null,
      gl_account_code: null,
      gl_account_name: null,
      document_number: null,
      line_item: null,
      posting_date: null,
      alert_code: null,
      writeback_status: null,
    },
  ],
  not_received: [
    {
      target_id: "10000000-0000-4000-8000-000000000003",
      company_id: "20000000-0000-4000-8000-000000000003",
      company_code: "4000",
      company_name: "尚未退税公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "50000.00",
      currency: "CNY",
      receipt_status: "NOT_RECEIVED",
      booking_status: "NOT_APPLICABLE",
      account_family: null,
      receipt_source: "SAP_MATCH",
      matched_amount: null,
      gl_account_code: null,
      gl_account_name: null,
      document_number: null,
      line_item: null,
      posting_date: null,
      alert_code: null,
      writeback_status: null,
    },
  ],
  ambiguous: [
    {
      target_id: "10000000-0000-4000-8000-000000000004",
      company_id: "20000000-0000-4000-8000-000000000004",
      company_code: "5000",
      company_name: "待人工确认公司",
      refund_tax_year: 2025,
      scan_period: "2026-03",
      expected_refund_amount: "30000.00",
      currency: "CNY",
      receipt_status: "AMBIGUOUS",
      booking_status: "AMBIGUOUS",
      account_family: null,
      receipt_source: "SAP_MATCH",
      matched_amount: null,
      gl_account_code: null,
      gl_account_name: null,
      document_number: null,
      line_item: null,
      posting_date: null,
      alert_code: "AMBIGUOUS_REFUND_MATCH",
      writeback_status: null,
    },
  ],
};

async function openRefundPage(page: Page) {
  await page.goto(
    `${entryUrl}?refund_tax_year=2025&scan_year=2026&scan_month=3`,
  );
  await page
    .getByRole("tab", {
      name: "所得税退税进度监控及入账科目准确性检查",
    })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "所得税退税进度监控及入账科目准确性检查",
    }),
  ).toBeVisible();
}

test("展示退税清单、入账详情和飞书回写状态", async ({ page }) => {
  let requestedPeriod = "";
  await page.route("**/api/v1/income-tax-refunds/results?**", async (route) => {
    const url = new URL(route.request().url());
    requestedPeriod = `${url.searchParams.get("refund_tax_year")}/${url.searchParams.get("scan_year")}/${url.searchParams.get("scan_month")}`;
    await route.fulfill({ json: refundResults });
  });

  await openRefundPage(page);

  await expect.poll(() => requestedPeriod).toBe("2025/2026/3");
  await expect(page.getByText("已正确入账公司")).toBeVisible();
  const wrongAccountRow = page
    .getByRole("row")
    .filter({ hasText: "错误科目公司" });
  await expect(wrongAccountRow).toContainText("CNY 80,000.00");
  await expect(wrongAccountRow).toContainText("6117990000");
  await expect(wrongAccountRow).toContainText("其他收益");
  await expect(wrongAccountRow).toContainText("510002 / 002");
  await expect(wrongAccountRow).toContainText("已退税但入账至其他收益");
  await expect(wrongAccountRow).toContainText("待回写");
  const taxesPayableRow = page
    .getByRole("row")
    .filter({ hasText: "应交税费错误入账公司" });
  await expect(taxesPayableRow).toContainText("已退税但入账至应交税费");
  await expect(taxesPayableRow).toContainText("2221130000");
  const manualRow = page
    .getByRole("row")
    .filter({ hasText: "飞书手工登记公司" });
  await expect(manualRow).toContainText("已退税（飞书已登记，停止扫描）");
  await expect(
    page.locator(".ant-statistic").filter({ hasText: "入账科目错误" }),
  ).toContainText("2");

  await page.getByRole("tab", { name: "未退税 (1)" }).click();
  await expect(page.getByText("尚未退税公司")).toBeVisible();
  await expect(page.getByText("CNY 50,000.00")).toBeVisible();

  await page.getByRole("tab", { name: "多个等额候选示警 (1)" }).click();
  const ambiguousRow = page
    .getByRole("row")
    .filter({ hasText: "待人工确认公司" });
  await expect(ambiguousRow).toContainText("存在多个等额候选，需人工确认");
});

test("接口失败时显示受控错误并允许重新加载", async ({ page }) => {
  let shouldFail = true;
  let requestCount = 0;
  await page.route("**/api/v1/income-tax-refunds/results?**", async (route) => {
    requestCount += 1;
    if (shouldFail) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "refund source unavailable" }),
      });
      return;
    }
    await route.fulfill({ json: refundResults });
  });

  await openRefundPage(page);

  await expect(page.getByText("退税监测结果加载失败")).toBeVisible();
  expect(requestCount).toBeGreaterThan(1);
  shouldFail = false;
  await page.getByRole("button", { name: "重新加载" }).click();
  await expect(page.getByText("已正确入账公司")).toBeVisible();
  await expect(page.getByText("退税监测结果加载失败")).toBeHidden();
});
