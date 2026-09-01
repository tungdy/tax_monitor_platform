import { expect, test } from "./test-fixture";

const welfareCaseId = "70000000-0000-4000-8000-000000000001";
const donationCaseId = "70000000-0000-4000-8000-000000000002";
const companyId = "80000000-0000-4000-8000-000000000001";

test("筛选并复核福利费和公益性捐赠风险", async ({ page }) => {
  let selectedMonitor = "BUSINESS_ENTERTAINMENT";
  let welfareStatus = "PENDING_COMPANY_CONFIRMATION";

  await page.route("**/api/v1/dashboard/quarterly**", async (route) => {
    await route.fulfill({
      json: {
        fiscal_year: 2026,
        quarter: 3,
        run_id: null,
        coverage_company_count: 0,
        data_ready_count: 0,
        blocked_count: 0,
        risk_company_count: 0,
        potential_tax_cost_total: "0",
        currency: "CNY",
        amount_scale: 2,
        monitoring_type_counts: {},
        companies: { total: 0, page: 1, page_size: 200, items: [] },
      },
    });
  });
  await page.route("**/api/v1/risk-cases?**", async (route) => {
    const url = new URL(route.request().url());
    selectedMonitor =
      url.searchParams.get("monitoring_type") ?? selectedMonitor;
    const risk =
      selectedMonitor === "WELFARE"
        ? welfareRisk(welfareStatus)
        : donationRisk();
    await route.fulfill({
      json:
        selectedMonitor === "WELFARE" || selectedMonitor === "DONATION"
          ? { total: 1, page: 1, page_size: 100, items: [risk] }
          : { total: 0, page: 1, page_size: 100, items: [] },
    });
  });
  await page.route(`**/api/v1/risk-cases/${welfareCaseId}`, async (route) => {
    await route.fulfill({ json: welfareDetail(welfareStatus) });
  });
  await page.route(`**/api/v1/risk-cases/${donationCaseId}`, async (route) => {
    await route.fulfill({ json: donationDetail() });
  });
  await page.route(
    `**/api/v1/risk-cases/${welfareCaseId}/actions`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        action: "REQUEST_ADJUSTMENT",
        to_status: "PENDING_ADJUSTMENT",
        reason: "确认科目入账风险，转入改账处理",
      });
      welfareStatus = "PENDING_ADJUSTMENT";
      await route.fulfill({
        json: {
          id: welfareCaseId,
          status: welfareStatus,
          assignee: "finance-reviewer",
          row_version: 2,
        },
      });
    },
  );

  await page.goto("/");
  await page.getByRole("tab", { name: "业务招待费风险" }).click();
  const monitorFilter = page.locator(".ant-select").filter({
    has: page.getByRole("combobox", { name: "监测类型" }),
  });
  await monitorFilter.click();
  await page.getByText("福利费", { exact: true }).click();
  await expect.poll(() => selectedMonitor).toBe("WELFARE");
  await page.getByRole("button", { name: "查看详情" }).click();
  const drawer = page.getByRole("dialog", { name: "所得税风险详情" });

  await expect(drawer.getByText("客户商务宴请")).toBeVisible();
  await expect(drawer.getByText("660205 职工福利费")).toBeVisible();
  await expect(
    drawer.getByText("800.00 CNY", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    drawer.getByText("BUSINESS_ENTERTAINMENT_EXPENSE"),
  ).toBeVisible();
  await expect(drawer.getByText("gold-model-v1")).toBeVisible();
  await expect(drawer.getByText("待公司确认")).toBeVisible();
  await expect(
    drawer.getByRole("button", { name: "要求补充证据" }),
  ).toBeVisible();
  await drawer.getByRole("button", { name: "确认风险" }).click();
  await expect(drawer.getByText("待改账")).toBeVisible();

  await page.locator(".ant-drawer-close").click();
  await monitorFilter.click();
  await page.getByText("公益性捐赠", { exact: true }).click();
  await expect.poll(() => selectedMonitor).toBe("DONATION");
  await page.getByRole("button", { name: "查看详情" }).click();

  await expect(drawer.getByText("活动冠名及品牌露出")).toBeVisible();
  await expect(drawer.getByText("671101 公益性捐赠")).toBeVisible();
  await expect(
    drawer.getByText("50000.00 CNY", { exact: true }).first(),
  ).toBeVisible();
  await expect(drawer.getByText("ADVERTISING_PROMOTION_EXPENSE")).toBeVisible();
  await expect(drawer.getByText("SAP凭证/行")).toBeVisible();
  await expect(drawer.getByText("gold-accounts-v1")).toBeVisible();
});

function welfareRisk(status = "PENDING_COMPANY_CONFIRMATION") {
  return {
    id: welfareCaseId,
    company_id: companyId,
    company_code: "1001",
    company_name: "福利费示例公司",
    monitoring_type: "WELFARE",
    risk_amount: "800.00",
    currency: "CNY",
    status,
    row_version: 1,
    fiscal_year: 2026,
    period: 6,
    source_mode: "SAP_LINKED",
    sap_link_status: "LINKED",
    sap_document_number: "510001",
    sap_line_item: "001",
    semantic_label: "BUSINESS_ENTERTAINMENT",
    confidence_tier: "HIGH",
    workflow_note: "已关联SAP凭证",
  };
}

function donationRisk() {
  return {
    ...welfareRisk(),
    id: donationCaseId,
    company_code: "1002",
    company_name: "捐赠示例公司",
    monitoring_type: "DONATION",
    risk_amount: "50000.00",
    sap_document_number: "610001",
    semantic_label: "ADVERTISING_PROMOTION",
  };
}

function welfareDetail(status: string) {
  return detailBase({
    caseId: welfareCaseId,
    companyCode: "1001",
    companyName: "福利费示例公司",
    monitoringType: "WELFARE",
    status,
    documentNumber: "510001",
    currentAccountCode: "660205",
    currentAccountName: "职工福利费",
    amount: "800.00",
    summary: "客户商务宴请",
    semanticLabel: "BUSINESS_ENTERTAINMENT",
    recommendedAccount: "BUSINESS_ENTERTAINMENT_EXPENSE",
  });
}

function donationDetail() {
  return detailBase({
    caseId: donationCaseId,
    companyCode: "1002",
    companyName: "捐赠示例公司",
    monitoringType: "DONATION",
    status: "PENDING_COMPANY_CONFIRMATION",
    documentNumber: "610001",
    currentAccountCode: "671101",
    currentAccountName: "公益性捐赠",
    amount: "50000.00",
    summary: "活动冠名及品牌露出",
    semanticLabel: "ADVERTISING_PROMOTION",
    recommendedAccount: "ADVERTISING_PROMOTION_EXPENSE",
  });
}

function detailBase(value: {
  caseId: string;
  companyCode: string;
  companyName: string;
  monitoringType: string;
  status: string;
  documentNumber: string;
  currentAccountCode: string;
  currentAccountName: string;
  amount: string;
  summary: string;
  semanticLabel: string;
  recommendedAccount: string;
}) {
  return {
    case_id: value.caseId,
    company_id: companyId,
    company_code: value.companyCode,
    company_name: value.companyName,
    monitoring_type: value.monitoringType,
    fiscal_year: 2026,
    period: 6,
    status: value.status,
    merged_into_case_id: null,
    canonical_source_record_id: "90000000-0000-4000-8000-000000000001",
    source_mode: "SAP_LINKED",
    sap_link_status: "LINKED",
    sap_document_number: value.documentNumber,
    sap_line_item: "001",
    sap_fiscal_year: 2026,
    current_account_code: value.currentAccountCode,
    current_account_name: value.currentAccountName,
    signed_amount: value.amount,
    risk_amount: value.amount,
    currency: "CNY",
    risk_amount_source: "SAP_VOUCHER_LINE",
    semantic_label: value.semanticLabel,
    confidence_tier: "HIGH",
    evidence_refs: [{ field_name: "summary", quoted_text: value.summary }],
    recommended_account_ids: [value.recommendedAccount],
    rationale_summary: "受控科目建议，仅供人工复核。",
    missing_evidence: [],
    rule_version_id: "gold-rule-v1",
    model_version_id: "gold-model-v1",
    prompt_version_id: "gold-prompt-v1",
    case_library_version_id: "gold-cases-v1",
    account_dictionary_version: "gold-accounts-v1",
    workflow_note: "已关联SAP凭证",
    row_version: 1,
    resolution_evidence_links: [],
  };
}
