import { readFile } from "node:fs/promises";

import type { MonitorResult } from "../src/features/full-validation/types";
import { fullValidationReport } from "./full-validation.fixture";
import { expect, test } from "./test-fixture";

const entryUrl = process.env.PLAYWRIGHT_ENTRY_URL ?? ".";

const capabilityNames = [
  "季度应计提所得税准确性检查",
  "递延所得税计提/转回准确性检查",
  "当年累计税负率异常监测",
  "潜在纳税调增税务成本",
  "纳税调增科目准确性检查",
  "所得税退税进度监控及入账科目准确性检查",
] as const;

test("shows the six-capability management dashboard", async ({ page }) => {
  const report = fullValidationReport;
  await page.goto(entryUrl);

  await expect(
    page.getByRole("heading", {
      name: "集团所得税风险监测驾驶舱",
      level: 2,
    }),
  ).toBeVisible();
  const scopeText = page.getByText(
    /2026年第2季度 · \d+家公司 · 6项已运行 \/ 0项待完善/,
  );
  await expect(scopeText).toBeVisible();
  const companyCount = Number(
    (await scopeText.textContent())?.match(/(\d+)家公司/)?.[1],
  );
  expect(companyCount).toBeGreaterThan(0);
  await expect(page.getByText("已运行能力", { exact: true })).toBeVisible();
  await expect(page.getByText("有示警公司")).toBeVisible();
  await expect(page.getByText("真实请求成功率")).toBeVisible();

  for (const name of capabilityNames) {
    await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
  }
  await expect(page.getByText("待完善", { exact: true })).toHaveCount(0);
  await expect(page.getByText(`当前 ${companyCount} 家`)).toBeVisible();
  await expect(
    page.getByRole("table").getByText("所得税税率", { exact: false }).first(),
  ).toBeVisible();

  const currentTaxCard = page.locator(".ant-card").filter({
    hasText: capabilityNames[0],
  });
  const alertCount = Number(
    await currentTaxCard.locator(".ant-statistic-content-value").textContent(),
  );
  expect(alertCount).toBeGreaterThan(0);

  const targetOutcome = report.companies
    .find((company) => company.monitor_results.current_tax_accrual?.outcome)
    ?.monitor_results.current_tax_accrual?.outcome.trim();
  expect(targetOutcome).toBeTruthy();
  const expectedOutcomeCount = report.companies.filter(
    (company) =>
      company.monitor_results.current_tax_accrual?.outcome.trim() ===
      targetOutcome,
  ).length;

  const outcomeFilter = page.getByLabel("检查结论").first();
  await outcomeFilter.click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content", {
    hasText: targetOutcome as string,
  }).click();
  await expect(page.getByText(`当前 ${expectedOutcomeCount} 家`)).toBeVisible();
  await outcomeFilter.click();
  await page
    .locator(".ant-select-dropdown:visible")
    .getByText("全部结论", { exact: true })
    .click();
  await expect(page.getByText(`当前 ${companyCount} 家`)).toBeVisible();

  await page.getByLabel("结果状态").first().click();
  await page.getByText("示警", { exact: true }).last().click();
  await expect(page.getByText(`当前 ${alertCount} 家`)).toBeVisible();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出明细" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(
    /^所得税风险监测_季度所得税_2026Q2_\d{14}\.csv$/,
  );
  const downloadPath = await download.path();
  expect(downloadPath).not.toBeNull();
  const csv = await readFile(downloadPath as string, "utf8");
  expect(csv.startsWith("\uFEFF")).toBe(true);
  expect(csv).toContain('"公司代码","公司名称","结果状态"');
  expect(csv).toContain(',"示警",');
  expect(csv).not.toContain(',"正常",');
});

test("cascades capability, status, and outcome filters", async ({ page }) => {
  const report = fullValidationReport;
  const currentTaxAlerts = report.companies
    .map((company) => company.monitor_results.current_tax_accrual)
    .filter(
      (result): result is MonitorResult => result?.status === "ALERT",
    );
  const refundResults = report.companies
    .map((company) => company.monitor_results.refund)
    .filter((result): result is MonitorResult => result !== undefined);
  const refundAlerts = refundResults.filter(
    (result) => result.status === "ALERT",
  );
  const refundAlertOutcome = refundAlerts[0]?.outcome.trim();
  const refundAlertOutcomeCount = refundAlerts.filter(
    (result) => result.outcome.trim() === refundAlertOutcome,
  ).length;
  const refundNonAlertOutcome = refundResults
    .find(
      (result) =>
        result.status !== "ALERT" &&
        !refundAlerts.some(
          (alertResult) => alertResult.outcome.trim() === result.outcome.trim(),
        ),
    )
    ?.outcome.trim();
  const currentTaxAlertOutcome = currentTaxAlerts[0]?.outcome.trim();
  expect(currentTaxAlertOutcome).toBeTruthy();
  expect(refundAlertOutcome).toBeTruthy();
  expect(refundNonAlertOutcome).toBeTruthy();

  await page.goto(entryUrl);
  const statusFilter = page.getByLabel("结果状态").first();
  const outcomeFilter = page.getByLabel("检查结论").first();

  await statusFilter.click();
  await page
    .locator(".ant-select-dropdown:visible")
    .getByText("示警", { exact: true })
    .click();
  await outcomeFilter.click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content", {
    hasText: currentTaxAlertOutcome as string,
  }).click();

  await page.getByLabel("监测能力").first().click();
  await page
    .getByText("所得税退税进度监控及入账科目准确性检查", { exact: true })
    .last()
    .click();
  await expect(statusFilter).toContainText("全部状态");
  await expect(outcomeFilter).toContainText("全部结论");

  await statusFilter.click();
  const statusDropdown = page.locator(".ant-select-dropdown:visible");
  const refundStatuses = new Set(refundResults.map((result) => result.status));
  for (const [status, label] of [
    ["ALERT", "示警"],
    ["CLEAR", "正常"],
    ["BLOCKED", "阻断"],
    ["NOT_APPLICABLE", "不适用"],
  ] as const) {
    const option = statusDropdown.getByText(label, { exact: true });
    if (refundStatuses.has(status)) {
      await expect(option).toBeVisible();
    } else {
      await expect(option).toHaveCount(0);
    }
  }
  await statusDropdown.getByText("示警", { exact: true }).click();
  await expect(page.getByText(`当前 ${refundAlerts.length} 家`)).toBeVisible();

  await outcomeFilter.click();
  const outcomeDropdown = page.locator(".ant-select-dropdown:visible");
  const refundAlertOption = outcomeDropdown.locator(
    ".ant-select-item-option-content",
    { hasText: refundAlertOutcome as string },
  );
  const refundNonAlertOption = outcomeDropdown.locator(
    ".ant-select-item-option-content",
    { hasText: refundNonAlertOutcome as string },
  );
  await expect(
    refundAlertOption,
  ).toBeVisible();
  await expect(refundNonAlertOption).toHaveCount(0);
  await refundAlertOption.click();
  await expect(
    page.getByText(`当前 ${refundAlertOutcomeCount} 家`),
  ).toBeVisible();

  await statusFilter.click();
  await page
    .locator(".ant-select-dropdown:visible")
    .getByText("正常", { exact: true })
    .click();
  await expect(outcomeFilter).toContainText("全部结论");
  await expect(
    page.getByText(
      `当前 ${refundResults.filter((result) => result.status === "CLEAR").length} 家`,
    ),
  ).toBeVisible();
});

test("shows full-company tax-adjustment account results and candidates", async ({
  page,
}) => {
  const report = structuredClone(fullValidationReport);
  const candidate = report.companies.find(
    (company) => company.company_code === "3CC0",
  )?.monitor_results.tax_adjustment_account_accuracy?.subject_results?.welfare
    ?.candidates?.[0];
  expect(candidate).toBeDefined();
  if (candidate) {
    candidate.hesi_detail_descriptions = "供应商公务接待报销";
    candidate.hesi_application_descriptions = "供应商来访招待申请";
  }
  await page.route("**/real-validation-latest.json?*", async (route) => {
    await route.fulfill({ json: report });
  });
  await page.goto(entryUrl);

  await page.getByLabel("监测能力").first().click();
  await page
    .getByText("纳税调增科目准确性检查", { exact: true })
    .last()
    .click();

  await expect(
    page.getByText(/本期真实结果覆盖业务招待费、福利费及公益性捐赠科目/),
  ).toBeVisible();
  await expect(
    page.getByText("业务招待费累计金额", { exact: false }).first(),
  ).toBeVisible();
  await page
    .getByLabel("检查科目")
    .getByText("福利费", { exact: true })
    .click();
  await expect(
    page.getByText("福利费累计金额", { exact: false }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("福利费纳税调增额", { exact: false }).first(),
  ).toBeVisible();

  await page.getByLabel("搜索公司").fill("3CC0");
  await expect(page.getByText("杭州海亮研学旅行有限公司")).toBeVisible();
  await expect(page.getByText("1 条", { exact: true })).toBeVisible();
  await page.locator(".ant-table-row-expand-icon").first().click();
  await expect(page.getByText(/供应商到访/)).toBeVisible();
  await expect(page.getByText("业务招待费异常", { exact: true })).toBeVisible();
  await expect(
    page.getByText(candidate?.header_text as string, { exact: false }),
  ).toBeVisible();
  const expandedCandidateTable = page.locator(".ant-table-expanded-row");
  await expect(
    expandedCandidateTable.getByText(candidate?.recommended_account as string, {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByText(/合思报销单事由：供应商公务接待报销/)).toBeVisible();
  await expect(
    page.getByText(/业务招待申请单事由：供应商来访招待申请/),
  ).toBeVisible();
  await expect(page.getByText(/行项目摘要命中关键词：供应商/)).toBeVisible();
  await expect(
    page.getByText("具体SAP科目编码按公司科目表确认", { exact: true }),
  ).toBeVisible();
});

test("shows real-source results or explicit blocks for potential tax cost", async ({
  page,
}) => {
  await page.goto(entryUrl);

  await page.getByLabel("监测能力").first().click();
  await page.getByText("潜在纳税调增税务成本", { exact: true }).last().click();

  await expect(page.getByRole("combobox", { name: "结果状态" })).toBeVisible();
  await expect(page.getByLabel("搜索公司")).toBeVisible();
  await expect(page.getByText(/当前 \d+ 家/).last()).toBeVisible();
  await expect(page.getByRole("table")).toBeVisible();
});

test("shows the updated deferred-tax formula results and inputs", async ({
  page,
}) => {
  const report = fullValidationReport;
  const expected = report.monitor_summary.deferred_tax;
  await page.goto(entryUrl);

  const deferredCard = page.locator(".ant-card").filter({
    hasText: capabilityNames[1],
  });
  await expect(deferredCard).toContainText(
    new RegExp(`示警\\s*${expected.ALERT}\\s*家`),
  );
  await expect(deferredCard).toContainText(
    new RegExp(`正常\\s*${expected.CLEAR}`),
  );
  await expect(deferredCard).toContainText(
    new RegExp(`阻断\\s*${expected.BLOCKED}`),
  );
  await deferredCard.getByRole("button", { name: "查看公司明细" }).click();

  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  for (const label of [
    "可弥补以前年度亏损",
    "损益表累计利润总额",
    "递延所得税计税基础",
    "递延所得税税率",
    "系统累计递延所得税",
    "SAP累计已计提",
    "应计提/转回",
  ]) {
    await expect(
      table.getByText(label, { exact: false }).first(),
    ).toBeVisible();
  }
});

test("keeps controls and results usable on a mobile viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(entryUrl);

  await expect(
    page.getByRole("heading", {
      name: "集团所得税风险监测驾驶舱",
      level: 2,
    }),
  ).toBeVisible();
  await expect(page.getByLabel("整体运行情况")).toBeVisible();
  await expect(page.getByRole("combobox", { name: "监测能力" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "检查结论" })).toBeVisible();
  await expect(page.getByLabel("搜索公司")).toBeVisible();
  await expect(page.getByRole("button", { name: "导出明细" })).toBeVisible();
  await expect(page.getByRole("table")).toBeVisible();
});
