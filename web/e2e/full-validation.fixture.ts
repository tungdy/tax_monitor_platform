import type {
  FullValidationReport,
  MonitorResult,
  SubjectMonitorResult,
  ValidationStatus,
} from "../src/features/full-validation/types";

function result(
  status: ValidationStatus,
  outcome: string,
  values: Record<string, string | null>,
  reason: string | null = null,
): MonitorResult {
  return { status, outcome, reason, values };
}

function subject(
  status: ValidationStatus,
  outcome: string,
  values: Record<string, string | null>,
  candidates: Record<string, string>[] = [],
): SubjectMonitorResult {
  return { status, outcome, reason: null, values, candidates };
}

export const fullValidationReport: FullValidationReport = {
  schema_version: 1,
  generated_at: "2026-07-01T08:00:00+08:00",
  fiscal_year: 2026,
  quarter: 2,
  through_period: 6,
  currency: "CNY",
  amount_scale: 2,
  source_mode: "REAL",
  company_scope: {
    base_record_count: 2,
    excluded_blank_company_count: 0,
    included_company_count: 2,
  },
  runtime: {
    parallelism: 2,
    cache: "test-fixture",
    external_fetch_seconds: 1.25,
    request_count: 12,
    request_error_count: 0,
    tax_adjustment_account_accuracy: {
      status: "DATA",
      company_count: 2,
      source_error_count: 0,
      candidate_company_count: 1,
      candidate_detail_count: 1,
      formula_evaluated_company_count: 2,
      business_entertainment_evaluated_company_count: 2,
      business_entertainment_alert_company_count: 1,
    },
  },
  refund_evidence_notice: "测试夹具覆盖退税到账与入账科目结论。",
  tax_adjustment_account_accuracy_notice:
    "本期真实结果覆盖业务招待费、福利费及公益性捐赠科目。",
  monitor_summary: {
    current_tax_accrual: {
      name: "季度应计提所得税准确性检查",
      total: 2,
      ALERT: 1,
      CLEAR: 1,
      BLOCKED: 0,
      NOT_APPLICABLE: 0,
    },
    deferred_tax: {
      name: "递延所得税计提/转回准确性检查",
      total: 2,
      ALERT: 1,
      CLEAR: 1,
      BLOCKED: 0,
      NOT_APPLICABLE: 0,
    },
    tax_burden: {
      name: "当年累计税负率异常监测",
      total: 2,
      ALERT: 1,
      CLEAR: 1,
      BLOCKED: 0,
      NOT_APPLICABLE: 0,
    },
    potential_tax_cost: {
      name: "潜在纳税调增税务成本",
      total: 2,
      ALERT: 1,
      CLEAR: 1,
      BLOCKED: 0,
      NOT_APPLICABLE: 0,
    },
    tax_adjustment_account_accuracy: {
      name: "纳税调增科目准确性检查",
      total: 2,
      ALERT: 1,
      CLEAR: 1,
      BLOCKED: 0,
      NOT_APPLICABLE: 0,
    },
    refund: {
      name: "所得税退税进度监控及入账科目准确性检查",
      total: 2,
      ALERT: 1,
      CLEAR: 1,
      BLOCKED: 0,
      NOT_APPLICABLE: 0,
    },
  },
  companies: [
    {
      company_code: "3CC0",
      company_name: "杭州海亮研学旅行有限公司",
      master_data_issues: [],
      monitor_results: {
        current_tax_accrual: result("ALERT", "季度所得税计提不足", {
          tax_rate: "0.25",
          cumulative_profit: "1000000",
          fair_value_change: "0",
          taxable_income: "1000000",
          system_current_tax: "250000",
          sap_current_tax: "200000",
          current_tax_difference: "50000",
        }),
        deferred_tax: result("ALERT", "递延所得税计提差异", {
          loss_carryforward: "100000",
          cumulative_profit: "50000",
          deferred_tax_base: "50000",
          deferred_tax_rate: "0.25",
          system_deferred_tax: "12500",
          sap_deferred_tax: "10000",
          deferred_tax_difference: "2500",
        }),
        tax_burden: result("CLEAR", "累计税负率正常", {
          current_tax_burden: "0.15",
          historical_tax_burden: "0.14",
          deviation: "0.01",
        }),
        potential_tax_cost: result("ALERT", "存在潜在税务成本", {
          estimated_expense: "30000",
          hesi_no_invoice: "20000",
          potential_tax_adjustment: "50000",
          potential_tax_cost: "12500",
        }),
        tax_adjustment_account_accuracy: {
          ...result("ALERT", "存在疑似错入科目", {}),
          subject_results: {
            business_entertainment: subject("ALERT", "业务招待费异常", {
              business_entertainment_cumulative: "30000",
              business_entertainment_detail_count: "2",
              business_entertainment_alert_count: "1",
              business_entertainment_alert_amount: "1000",
            }),
            welfare: subject(
              "ALERT",
              "福利费存在疑似错入",
              {
                welfare_cumulative: "80000",
                salary_cumulative: "400000",
                welfare_deduction_limit: "56000",
                welfare_adjustment: "24000",
                welfare_detail_selected: "true",
                welfare_abnormal_candidate_count: "1",
                welfare_alert_count: "1",
                welfare_alert_amount: "1000",
              },
              [
                {
                  subject: "业务招待费异常",
                  fiscal_period: "2026-06",
                  voucher_no: "510001",
                  gl_account: "660200",
                  account_name: "管理费用",
                  recommended_account: "职工福利费",
                  header_text: "供应商到访",
                  detail_text: "供应商接待支出",
                  amount: "1000.00",
                  recommendation_basis: "行项目摘要命中关键词：供应商",
                },
              ],
            ),
            donation: subject("CLEAR", "公益性捐赠科目正常", {
              donation_cumulative: "10000",
              donation_abnormal_candidate_count: "0",
              donation_alert_count: "0",
            }),
          },
        },
        refund: result("ALERT", "已退税但入账至其他收益", {
          expected_refund_amount: "80000",
          matched_amount: "80000",
          booking_account: "6117990000",
          booking_account_family: "OTHER_INCOME",
          receipt_source: "SAP_MATCH",
        }),
      },
    },
    {
      company_code: "C002",
      company_name: "示例正常公司",
      master_data_issues: [],
      monitor_results: {
        current_tax_accrual: result("CLEAR", "季度所得税计提一致", {
          tax_rate: "0.25",
          cumulative_profit: "800000",
          fair_value_change: "0",
          taxable_income: "800000",
          system_current_tax: "200000",
          sap_current_tax: "200000",
          current_tax_difference: "0",
        }),
        deferred_tax: result("CLEAR", "递延所得税计提一致", {
          loss_carryforward: "0",
          cumulative_profit: "100000",
          deferred_tax_base: "0",
          deferred_tax_rate: "0.25",
          system_deferred_tax: "0",
          sap_deferred_tax: "0",
          deferred_tax_difference: "0",
        }),
        tax_burden: result("ALERT", "累计税负率偏高", {
          current_tax_burden: "0.2",
          historical_tax_burden: "0.1",
          deviation: "0.1",
        }),
        potential_tax_cost: result("CLEAR", "未发现潜在税务成本", {
          estimated_expense: "0",
          hesi_no_invoice: "0",
          potential_tax_adjustment: "0",
          potential_tax_cost: "0",
        }),
        tax_adjustment_account_accuracy: {
          ...result("CLEAR", "调增科目正常", {}),
          subject_results: {
            business_entertainment: subject("CLEAR", "业务招待费科目正常", {
              business_entertainment_cumulative: "10000",
              business_entertainment_detail_count: "1",
              business_entertainment_alert_count: "0",
              business_entertainment_alert_amount: "0",
            }),
            welfare: subject("CLEAR", "福利费科目正常", {
              welfare_cumulative: "20000",
              salary_cumulative: "400000",
              welfare_deduction_limit: "56000",
              welfare_adjustment: "0",
              welfare_detail_selected: "true",
              welfare_abnormal_candidate_count: "0",
              welfare_alert_count: "0",
              welfare_alert_amount: "0",
            }),
            donation: subject("CLEAR", "公益性捐赠科目正常", {
              donation_cumulative: "0",
              donation_abnormal_candidate_count: "0",
              donation_alert_count: "0",
            }),
          },
        },
        refund: result("CLEAR", "已退税且入账正确", {
          expected_refund_amount: "50000",
          matched_amount: "50000",
          booking_account: "6801010000",
          booking_account_family: "INCOME_TAX_EXPENSE",
          receipt_source: "SAP_MATCH",
        }),
      },
    },
  ],
};
