import type { Locator, Page } from "@playwright/test";

import { installQuarterlyDashboardMock } from "./quarterly-dashboard.fixture";
import { expect, test } from "./test-fixture";

const FISCAL_YEAR = 2026;
const QUARTER = 2;

function statistic(container: Page | Locator, title: string) {
  return container
    .locator(".ant-statistic-title")
    .getByText(title, { exact: true })
    .locator("..");
}

function descriptionValue(container: Locator, label: string) {
  return container
    .getByText(label, { exact: true })
    .locator("xpath=ancestor::tr[1]")
    .locator(".ant-descriptions-item-content");
}

test.describe.configure({ mode: "serial" });

test.describe("季度所得税风险看板", () => {
  test("展示105家公司监测结果并可追溯标准公司的计提公式", async ({ page }) => {
    const externalCompanyCode = process.env.E2E_STANDARD_COMPANY_CODE;
    const standardCompanyCode = externalCompanyCode ?? "E2E-mock-000";
    if (
      !standardCompanyCode.startsWith("E2E-") ||
      !standardCompanyCode.endsWith("-000")
    ) {
      throw new Error(
        "E2E_STANDARD_COMPANY_CODE must use E2E-<seed-token>-000",
      );
    }
    const seedToken = standardCompanyCode.slice(4, -4);
    if (externalCompanyCode === undefined) {
      await installQuarterlyDashboardMock(page, standardCompanyCode);
    }

    await page.goto(`/?fiscal_year=${FISCAL_YEAR}&quarter=${QUARTER}`);

    await expect(page).toHaveURL(
      new RegExp(`fiscal_year=${FISCAL_YEAR}&quarter=${QUARTER}`),
    );
    await page.getByRole("tab", { name: "季度所得税监测" }).click();
    await expect(
      page.getByRole("heading", { name: "季度所得税风险看板" }),
    ).toBeVisible();
    await expect(page.getByRole("combobox", { name: "年度" })).toBeVisible();
    await expect(page.getByRole("combobox", { name: "季度" })).toBeVisible();

    const quarterlyPanel = page.getByRole("tabpanel", {
      name: "季度所得税监测",
    });
    await expect(statistic(quarterlyPanel, "覆盖公司")).toContainText("105");
    await expect(statistic(quarterlyPanel, "数据就绪")).toContainText("105");
    await expect(statistic(quarterlyPanel, "数据质量阻断")).toContainText("2");
    await expect(statistic(quarterlyPanel, "异常公司")).toContainText("103");
    await expect(statistic(quarterlyPanel, "潜在风险估算")).toContainText(
      "¥43,775,000.00",
    );

    const qualityRegion = page.getByRole("region", {
      name: "数据质量阻断",
    });
    await expect(qualityRegion).toContainText(
      "2家公司因数据质量问题未进入风险计算",
    );
    await expect(qualityRegion.getByText("阻断", { exact: true })).toHaveCount(
      2,
    );

    const riskRegion = page.getByRole("region", { name: "风险清单" });
    const accrualRow = riskRegion
      .getByRole("row")
      .filter({ hasText: standardCompanyCode })
      .filter({ hasText: "所得税计提准确性" });

    await expect(accrualRow).toHaveCount(1);
    await expect(accrualRow).toContainText("E2E Quarterly Company 000");
    await expect(accrualRow).toContainText("少计提");
    await expect(accrualRow).toContainText("¥700,000.00");
    await expect(accrualRow).toContainText("¥725,000.00");
    await expect(accrualRow).toContainText("+¥25,000.00");

    await accrualRow.getByRole("button", { name: "查看公式" }).click();

    const formulaDrawer = page.getByRole("dialog", {
      name: "公式与数据血缘",
    });
    await expect(formulaDrawer).toBeVisible();
    await expect(descriptionValue(formulaDrawer, "累计利润总额")).toContainText(
      "¥10,000,000.00",
    );
    await expect(descriptionValue(formulaDrawer, "累计收到分红")).toContainText(
      "¥1,000,000.00",
    );
    await expect(
      descriptionValue(formulaDrawer, "累计公允价值变动损益"),
    ).toContainText("¥500,000.00");
    await expect(
      descriptionValue(formulaDrawer, "可弥补以前年度亏损"),
    ).toContainText("¥2,000,000.00");
    await expect(descriptionValue(formulaDrawer, "适用税率")).toContainText(
      "25%",
    );
    await expect(
      descriptionValue(formulaDrawer, "本年累计应纳税额"),
    ).toContainText("¥1,625,000.00");
    await expect(
      descriptionValue(formulaDrawer, "以前季度SAP所得税计提"),
    ).toContainText("¥900,000.00");
    await expect(
      descriptionValue(formulaDrawer, "本季度应计提所得税额"),
    ).toContainText("¥725,000.00");
    await expect(
      descriptionValue(formulaDrawer, "本季度SAP所得税计提"),
    ).toContainText("¥700,000.00");
    await expect(
      descriptionValue(formulaDrawer, "本季度所得税计提差异"),
    ).toContainText("+¥25,000.00");

    await expect(descriptionValue(formulaDrawer, "来源系统")).toContainText(
      "SAP",
    );
    await expect(descriptionValue(formulaDrawer, "来源批次")).toContainText(
      `e2e-${seedToken}-sap-quarterly`,
    );
    await expect(descriptionValue(formulaDrawer, "取数时间")).toContainText(
      "2026-07-03T08:00:00Z",
    );
    await expect(
      descriptionValue(formulaDrawer, "Snapshot校验值"),
    ).toContainText(/^[0-9a-f]{64}$/);
    await expect(
      descriptionValue(formulaDrawer, "税务主数据版本"),
    ).toContainText(/^[0-9a-f]{20}-r2$/);
    await expect(descriptionValue(formulaDrawer, "主数据文件")).toContainText(
      /quarterly-master-.*\.xlsx/,
    );
    await expect(descriptionValue(formulaDrawer, "规则版本")).toContainText(
      "deferred-tax-loss-less-profit-reviewed",
    );

    await formulaDrawer.getByRole("button", { name: "Close" }).click();
    await expect(formulaDrawer).not.toBeVisible();

    const deferredRow = riskRegion
      .getByRole("row")
      .filter({ hasText: standardCompanyCode })
      .filter({ hasText: "递延所得税计提/转回准确性" });
    await expect(deferredRow).toHaveCount(1);
    await expect(deferredRow).toContainText("应转回");
    await expect(deferredRow).toContainText("¥2,000,000.00");
    await expect(deferredRow).toContainText("-¥1,600,000.00");
    await expect(deferredRow).toContainText("-¥3,600,000.00");
    await expect(deferredRow).toContainText("¥10,000,000.00");
    await expect(deferredRow).toContainText("20%");

    await deferredRow.getByRole("button", { name: "查看公式" }).click();
    await expect(formulaDrawer).toBeVisible();
    await expect(descriptionValue(formulaDrawer, "示警结论")).toContainText(
      "应转回递延所得税",
    );
    await expect(
      descriptionValue(formulaDrawer, "可弥补以前年度亏损"),
    ).toContainText("¥2,000,000.00");
    await expect(
      descriptionValue(formulaDrawer, "损益表累计利润总额"),
    ).toContainText("¥10,000,000.00");
    await expect(
      descriptionValue(formulaDrawer, "递延所得税计税基础"),
    ).toContainText("-¥8,000,000.00");
    await expect(
      descriptionValue(formulaDrawer, "递延所得税税率"),
    ).toContainText("20%");
    await expect(
      descriptionValue(formulaDrawer, "系统累计递延所得税费用"),
    ).toContainText("-¥1,600,000.00");
    await expect(
      descriptionValue(formulaDrawer, "SAP累计已计提的递延所得税费用"),
    ).toContainText("¥2,000,000.00");
    await expect(
      descriptionValue(formulaDrawer, "本年应计提/转回的递延所得税费用"),
    ).toContainText("-¥3,600,000.00");
  });
});
