import { expect, test } from "./test-fixture";

const caseId = "10000000-0000-4000-8000-000000000001";
const rootCaseId = "10000000-0000-4000-8000-000000000002";
const companyId = "20000000-0000-4000-8000-000000000001";
const evidenceLinkId = "30000000-0000-4000-8000-000000000001";

test("筛选、证据复核、精确关联解决、根案件刷新和SAP覆盖", async ({ page }) => {
  let resolved = false;
  let selectedSourceMode = "";

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
    if (url.searchParams.get("monitoring_type") === "BUSINESS_ENTERTAINMENT") {
      selectedSourceMode =
        url.searchParams.get("source_mode") ?? selectedSourceMode;
      await route.fulfill({
        json: {
          total: 1,
          page: 1,
          page_size: 100,
          items: [
            {
              id: resolved ? rootCaseId : caseId,
              company_id: companyId,
              company_code: "C001",
              company_name: "示例公司",
              monitoring_type: "BUSINESS_ENTERTAINMENT",
              risk_amount: "1280.00",
              currency: "CNY",
              status: "NEW",
              row_version: 1,
              fiscal_year: 2026,
              period: 7,
              source_mode: resolved
                ? "SAP_LINKED"
                : "BUSINESS_DOCUMENT_UNLINKED",
              sap_link_status: resolved ? "LINKED" : "PENDING_LOCATION",
              sap_document_number: resolved ? "510001" : null,
              sap_line_item: resolved ? "001" : null,
              semantic_label: "EMPLOYEE_EDUCATION",
              confidence_tier: "HIGH",
              workflow_note: resolved ? "已关联SAP凭证" : "待定位SAP凭证",
            },
          ],
        },
      });
      return;
    }
    await route.fulfill({
      json: { total: 0, page: 1, page_size: 200, items: [] },
    });
  });
  await page.route(`**/api/v1/risk-cases/${caseId}`, async (route) => {
    await route.fulfill({
      json: {
        case_id: caseId,
        company_id: companyId,
        company_code: "C001",
        company_name: "示例公司",
        status: "NEW",
        merged_into_case_id: resolved ? rootCaseId : null,
        canonical_source_record_id: "40000000-0000-4000-8000-000000000001",
        source_mode: "BUSINESS_DOCUMENT_UNLINKED",
        sap_link_status: "PENDING_LOCATION",
        sap_document_number: null,
        sap_line_item: null,
        risk_amount: "1280.00",
        currency: "CNY",
        risk_amount_source: "BUSINESS_DOCUMENT",
        semantic_label: "EMPLOYEE_EDUCATION",
        confidence_tier: "HIGH",
        evidence_refs: [
          { field_name: "申请事由", quoted_text: "内部培训班会议餐" },
        ],
        recommended_account_ids: ["EMPLOYEE_EDUCATION"],
        rationale_summary: "现有证据显示可能更符合职工教育经费。",
        missing_evidence: [],
        rule_version_id: "rule-v1",
        model_version_id: "model-v1",
        prompt_version_id: "prompt-v1",
        case_library_version_id: "cases-v1",
        account_dictionary_version: "accounts-v1",
        workflow_note: "待定位SAP凭证",
        row_version: resolved ? 2 : 1,
        resolution_evidence_links: [
          {
            evidence_link_id: evidenceLinkId,
            relation_quality: "EXACT",
            matched_field: "reference",
            sap_document_number: "510001",
            sap_line_item: "001",
          },
        ],
      },
    });
  });
  await page.route(
    `**/api/v1/business-entertainment/risk-cases/${caseId}/resolve-to-sap`,
    async (route) => {
      expect(route.request().postDataJSON()).toEqual({
        evidence_link_id: evidenceLinkId,
        expected_row_version: 1,
      });
      resolved = true;
      await route.fulfill({
        json: {
          source_case_id: caseId,
          root_case_id: rootCaseId,
          evidence_link_id: evidenceLinkId,
          merged: true,
        },
      });
    },
  );
  await page.route(
    "**/api/v1/business-entertainment/sap-link-coverage**",
    async (route) => {
      await route.fulfill({
        json: {
          total: 1,
          items: [
            {
              coverage_id: "50000000-0000-4000-8000-000000000001",
              company_id: companyId,
              company_code: "C001",
              company_name: "示例公司",
              period: "2026-07-31",
              document_number: "510099",
              line_item: "009",
              amount: "880.00",
              currency: "CNY",
              link_status: "UNLINKED",
              exact_evidence_link_id: null,
              evaluated_via_business_document: false,
              snapshot_id: "60000000-0000-4000-8000-000000000001",
            },
          ],
        },
      });
    },
  );

  await page.goto("/");
  await page.getByRole("tab", { name: "业务招待费风险" }).click();
  await expect(
    page.getByRole("heading", { name: "所得税风险清单" }),
  ).toBeVisible();
  await expect(page.getByText("待定位SAP凭证")).toBeVisible();
  await expect(page.getByRole("link", { name: "导出Excel" })).toHaveAttribute(
    "href",
    "/api/v1/exports/business-entertainment.xlsx",
  );

  await page.getByRole("combobox", { name: "来源模式" }).click();
  await page.getByText("业务单据未关联", { exact: true }).click();
  await expect
    .poll(() => selectedSourceMode)
    .toBe("BUSINESS_DOCUMENT_UNLINKED");

  await page.getByRole("button", { name: "查看详情" }).click();
  await expect(page.getByText("内部培训班会议餐")).toBeVisible();
  await expect(
    page.getByLabel("改账建议").getByText("EMPLOYEE_EDUCATION"),
  ).toBeVisible();
  await page.getByRole("button", { name: "关联SAP凭证" }).click();
  await expect(page.getByText("510001 / 001（精确关联）")).toBeVisible();
  await page.getByRole("button", { name: "确认解决" }).click();
  await expect(page.getByText("已关联SAP凭证")).toBeVisible();

  await page.locator(".ant-drawer-close").click();
  await page.getByRole("tab", { name: "SAP关联覆盖" }).click();
  await expect(page.getByText("510099")).toBeVisible();
  await expect(page.getByText("未关联前置单据")).toBeVisible();
  await expect(
    page.getByText("仅形成覆盖观察，不进入Agent语义判断"),
  ).toBeVisible();
});
