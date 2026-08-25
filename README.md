# 🧾 集团所得税风险监测平台

[![Version](https://img.shields.io/badge/version-0.4.0-blue.svg)](CHANGELOG.md)
[![Technical Ready](https://img.shields.io/badge/technical__ready-true-success.svg)](docs/operations/acceptance-scorecard.md)
[![Evidence](https://img.shields.io/badge/evidence-LOCAL__SYNTHETIC-orange.svg)](artifacts/acceptance/phase-4/uat-scorecard.json)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB.svg)](backend/pyproject.toml)
[![React](https://img.shields.io/badge/React-18-61DAFB.svg)](web/package.json)

面向 100 家以上集团公司的所得税风险监测平台，将汇算清缴时集中发现的问题前移到月度或季度自动监测。平台具备六项核心能力：按季度检查所得税计提、递延所得税计提/转回、累计税负和潜在纳税调增成本，按月识别业务招待费、福利费和公益性捐赠疑似错入明细，并在每年 3-12 月执行“所得税退税进度监控及入账科目准确性检查”，形成风险清单、证据链和改账建议。

当前已完成第一至第四阶段的离线可开发实现。现有验收证据范围为 `LOCAL_SYNTHETIC`：可以证明技术门禁通过，但不能替代真实公司试点、生产 KMS/HSM 签名和集团税务、数据、安全、运维四方批准。

---

## 🎯 监测能力

| 监测环节 | 频率 | 核心判断 | 主要输出 |
|---|---|---|---|
| 所得税计提准确性 | 季度 | 系统测算本季度应计提额与 SAP 实际计提额是否一致 | 差异公司、应计提额、实际计提额、计提差异 |
| 递延所得税计提/转回准确性 | 季度 | 系统累计递延所得税费用与 SAP 累计已计提的递延所得税费用是否一致 | 应计提或转回公司、本年应计提/转回额、亏损、累计利润、SAP 累计已计提额 |
| 累计税负率异常 | 季度 | 当年累计税负率与线下表维护的前三个完整年度平均税负率偏离绝对值是否达到 5% | 异常公司、当年税负率、历史平均税负率、偏离度 |
| 潜在纳税调增成本 | 季度 | 其他应付款暂估与合思无票报销形成的潜在调增是否产生所得税成本 | 潜在调增金额、潜在应纳税额、潜在税务成本 |
| 纳税调增科目准确性 | 月度 | 业务招待费、福利费和公益性捐赠明细是否错入当前科目 | 疑似错入明细、证据、置信度、改账建议、复核任务 |
| 所得税退税进度监控及入账科目准确性检查 | 每年 3-12 月每月 | 退税所属年 N 的应退金额是否在扫描年 N+1 的 SAP 单条贷方未冲销明细中唯一等额命中，且命中科目是否为所得税费用 | 已退税公司清单、未退税公司清单、所得税退税金额、所得税退税入账科目 |

适用税率、递延所得税税率、可弥补以前年度亏损和前三个完整年度平均税负率均来自受控线下表，平台只按公司和有效期匹配，不自行判断税率或重新计算历史平均税负率。递延所得税税率是独立的公司级主数据字段，不默认等同于适用税率。累计分红金额取 SAP 账上收到分红的金额。

本年累计所得税税负率原则上按“本年累计应纳税额÷损益表累计营业收入”计算；损益表累计营业收入小于或等于0时，税负率直接取0，并继续与历史平均税负率计算偏离度和判断是否示警。

季度递延所得税准确性检查采用以下口径，不对计税基础取零，并保留用户确认的“可弥补以前年度亏损 + 损益表累计利润总额”加法语义：

```text
系统累计递延所得税费用
=（可弥补以前年度亏损 + 损益表累计利润总额）
  × 递延所得税税率

本年应计提/转回的递延所得税费用
= 系统累计递延所得税费用
- SAP累计已计提的递延所得税费用
```

本年应计提/转回额按公司账簿币种精度四舍五入后不为0即示警；正数表示应计提，负数表示应转回。SAP 累计已计提递延所得税费用按科目余额表 `1811030000`（递延所得税资产-可抵扣亏损）的期末余额原值取数，不做符号翻转；科目余额表查询成功且未找到该科目时，视为该公司未计提并取0。整个接口调用失败、响应不合法或未导入时仍按缺数据阻断。

季度潜在税务成本采用以下口径，保留加号语义：

```text
潜在调增金额
= 其他应付款暂估余额
+ 合思无票报销金额

本年累计潜在应计提所得税额
= max（损益表累计利润总额
- 累计分红金额
- 累计公允价值变动损益
- 可弥补以前年度亏损
+ 潜在调增金额, 0）
  × 适用税率

潜在税务成本
= 本年累计潜在应计提所得税额 - 本年累计应纳税额
```

完整公式、范围和异常处理见[功能设计说明书](docs/design/function/2026-07-12-group-income-tax-risk-monitoring-platform-function.md)。

## 🤖 Agent 工作流

### 季度监测

```text
SAP及线下税务主数据
→ 数据质量和有效期校验
→ 冻结公司快照集
→ 确定性公式逐公司测算
→ 当期所得税计提/递延所得税/税负/潜在成本风险事项
→ 公司复核、处理与审计留痕
```

### 月度科目监测

```text
SAP凭证及OA/合思业务证据
→ 公司范围门禁和候选宽筛
→ 证据关联及最小必要字段读取
→ 业务招待费/福利费/捐赠专业Agent深判
→ 风险清单和改账建议
→ 公司财务复核、集团税务关闭
```

业务招待费支持三种证据路径：

- SAP 凭证能关联 OA 或合思单据时，以 SAP 凭证行为主记录并引用前置业务证据；
- OA 或合思单据不能关联 SAP 时，仍根据申请事由等字段独立判断，同时标记“待定位 SAP 凭证”；
- 仅有 SAP 凭证但找不到精确前置单据时，记录覆盖状态，不把未关联本身直接判定为错账。

福利费只有在“累计福利费－累计工资薪金×14%＞0”时进入明细检查；公益性捐赠只有在“累计公益性捐赠－累计利润总额×12%＞0”时进入明细检查。

### 所得税退税进度监控及入账科目准确性检查

```text
飞书应退税公司及金额（退税所属年 N）
→ 扫描年 N+1 的 3-12 月按月拉取 SAP 候选凭证行
→ 按公司币种和 amount scale 使用 ROUND_HALF_UP 量化
→ 仅在贷方、未冲销的单条明细中完全等额匹配
→ 到账状态、入账科目结论、风险案件和幂等回写 outbox
```

- 没有等额候选时为 `NOT_RECEIVED`，不示警且次月继续扫描；
- 唯一命中所得税费用时为 `RECEIVED + CORRECT`，停止后续扫描并安排回写“已退税”；
- 唯一命中其他收益时为 `RECEIVED + WRONG_ACCOUNT`，停止后续扫描、安排回写并生成 `REFUND_BOOKED_TO_WRONG_ACCOUNT` 风险；
- 多个等额候选时为 `AMBIGUOUS` 并示警，生成风险案例但不自动回写，次月继续扫描。

下月是否跳过以平台数据库中“公司 + 退税所属年”的本地 `RECEIVED` 状态为准，飞书状态只是异步同步结果，不能替代平台主事实。

## 🧪 代表性验收场景

### 季度标准公司

标准样例使用累计利润总额 1,000 万元、收到分红 100 万元、公允价值变动损益 50 万元、可弥补亏损 200 万元、适用税率和递延所得税税率均为 25%、SAP 累计已计提递延所得税费用 280 万元、累计营业收入 5,000 万元和历史平均税负率 9%。预期结果为：

- 本年累计应纳税额 162.50 万元；
- 本季度应计提 72.50 万元，SAP 实际计提 70 万元，少计提 2.50 万元；
- 系统累计递延所得税费用 300 万元，本年仍应计提 20 万元；
- 当年累计税负率 3.25%，偏离度 -5.75%，触发税负偏低提示；
- 潜在调增 170 万元，潜在应纳税额 205 万元，潜在税务成本 42.50 万元。

### 月度典型科目

| 当前科目与摘要/事由 | 预期判断 | 建议 |
|---|---|---|
| 业务招待费：内部培训班会议餐 | 疑似错入 | 建议转职工教育经费 |
| 业务招待费：内部季度会议工作餐 | 疑似错入 | 建议转会议费 |
| 业务招待费：接待外部客户商务晚宴 | 当前科目合理 | 无需改账 |
| 未关联 SAP 的 OA 员工团建聚餐 | 疑似错入 | 定位 SAP 后建议转福利费 |
| 福利费：客户商务宴请 | 疑似错入 | 建议转业务招待费 |
| 福利费：员工培训费 | 疑似错入 | 建议转职工教育经费 |
| 福利费：员工年度体检 | 当前科目合理 | 无需改账 |
| 公益性捐赠：活动冠名及品牌露出 | 疑似错入 | 建议转广告宣传费 |
| 公益性捐赠：无对价公益捐赠且材料完整 | 当前科目合理 | 无需改账 |

退税验收至少覆盖无候选、唯一所得税费用、唯一其他收益、第一阶段零命中后的唯一应交税费、第一阶段优先级、飞书手工已退税停扫和多个等额候选；金额比较必须验证四舍五入边界、币种、scale、贷方及冲销标志，不能使用金额容差或多行求和替代单条完全等额。

本地验收还覆盖 105 家公司批次中 103 家成功、2 家因受控主数据问题阻断，以及 126 家公司容量场景。完整样例和阈值见[第四阶段验收评分卡](docs/operations/acceptance-scorecard.md)。

## 📊 当前技术验收

| 指标 | 本地合成证据结果 |
|---|---:|
| 公式准确率 | 100% |
| 可追溯率 | 100% |
| 主数据缺陷阻断率 | 100% |
| 有效公司成功率 | 100% |
| 语义召回率 | 96% |
| 高置信度准确率 | 82% |
| 已知典型案例漏检 | 0 |
| 容量验收 | 126 家公司 |

当前自动评分为 `technical_ready=true`、`production_ready=false`。任何真实批准或生产证据缺失时，发布决策必须保持 `NO-GO`。

## 📁 仓库结构

```text
.
├── backend/                  # FastAPI、PostgreSQL、Celery、公式与语义Agent
├── web/                      # React季度驾驶舱、月度风险、导出和运维页面
├── infra/                    # Compose、可观测性、验证脚本和中文运行手册
├── docs/
│   ├── design/              # 架构、功能与详细设计
│   ├── operations/          # 验收评分卡、数据负责人清单和用户培训
│   └── superpowers/         # 经确认的设计规格与实施计划
├── artifacts/acceptance/    # 本地验收生成的可复核证据
├── Makefile                 # 统一验证、发布和回滚入口
├── CHANGELOG.md             # 阶段版本历史
└── README.md
```

详细模块边界见[架构设计说明书](docs/design/architecture/2026-07-12-group-income-tax-risk-monitoring-platform-architecture.md)。

## 🚀 本地启动

准备环境并启动数据服务：

```bash
cp infra/env.example infra/.env
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d postgres redis database-roles
export MIGRATION_DATABASE_URL='postgresql+psycopg://tax_risk_owner:replace-for-local-development-only@127.0.0.1:5432/tax_risk'
export DATABASE_URL='postgresql+psycopg://tax_risk_app:replace-for-local-application-only@127.0.0.1:5432/tax_risk'
export TEST_DATABASE_URL="$MIGRATION_DATABASE_URL"
export REDIS_URL='redis://127.0.0.1:6379/0'
```

安装后端和前端依赖：

```bash
uv venv backend/.venv
uv pip install --python backend/.venv/bin/python -e 'backend[dev]'
cd web && npm ci && cd ..
```

迁移数据库并启动完整本地技术栈：

```bash
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
docker compose --env-file infra/.env -f infra/docker-compose.yml up -d --build
```

Web 默认地址为 `http://127.0.0.1:8080/`，API 为 `http://127.0.0.1:8000/`。本地固定身份只能用于隔离开发机；生产必须关闭开发身份并接入批准的 IdP。

### 登录配置

平台支持本地账号密码和飞书 OAuth 授权登录。浏览器会话使用签名的 `HttpOnly` Cookie；生产环境自动启用 `Secure`，账号权限仍复用现有角色、公司范围和 PostgreSQL RLS。

先生成本地账号的 scrypt 密码哈希：

```bash
backend/.venv/bin/python backend/scripts/hash_login_password.py
```

Windows PowerShell 使用：

```powershell
backend\.venv\Scripts\python.exe backend\scripts\hash_login_password.py
```

将输出写入受 Git 忽略的 `infra/.env`，密码不得明文保存：

```dotenv
AUTH_SESSION_SECRET=<至少32字符的独立随机密钥>
AUTH_LOCAL_ACCOUNTS={"tax.admin":{"password_hash":"<上一步输出>","subject":"local:tax.admin","display_name":"税务管理员","roles":["group-tax"],"allowed_company_ids":[],"organization_path":"/group/tax"}}
DEVELOPMENT_PRINCIPAL_ENABLED=false
```

启用飞书登录时，在飞书开放平台登记精确回调地址 `https://<平台域名>/api/v1/auth/feishu/callback`，再配置应用凭据、允许的租户和用户映射：

```dotenv
AUTH_FEISHU_ENABLED=true
AUTH_FEISHU_CLIENT_ID=<飞书应用ID>
AUTH_FEISHU_CLIENT_SECRET=<飞书应用Secret>
AUTH_FEISHU_REDIRECT_URI=https://<平台域名>/api/v1/auth/feishu/callback
AUTH_FEISHU_TENANT_KEY=<允许登录的租户tenant_key>
AUTH_FEISHU_PRINCIPALS={"ou_xxx":{"subject":"feishu:ou_xxx","display_name":"税务用户","roles":["group-tax"],"allowed_company_ids":[],"organization_path":"/group/tax"}}
```

飞书授权采用 OAuth v3 授权码流程、PKCE `S256` 和一次性 `state` 校验；应用 Secret、用户令牌均只保留在 API 服务端。用户授权以 `tenant_key + open_id` 为准，不使用邮箱或手机号作为登录凭据。

## SAP 利润表 DGC 接口

平台按真实接口 `https://116.63.221.181/post/sapincome` 拉取 SAP 利润表。默认使用 DGC AppKey/AppSecret 对完整请求体执行华为 APIG `SDK-HMAC-SHA256` 签名，并发送 `X-Sdk-Date`、`Authorization` 和 `x-Authorization`；IAM Token 方式仅作为显式兼容配置。连接器管理 `limitValue`、`offsetValue` 分页，默认每页 `15000` 条。认证拒绝、`DLM.4018` 及其他远端或响应格式错误会直接失败，不写入半批数据；IAM 模式遇到 HTTP 401/403 或 `DLM.4211` 时只刷新 Token 并重试一次。单页字节数、总记录数和最大页数均有硬上限，远端错误详情不会原样返回调用方。

启用前在部署密钥管理和环境配置中提供以下值：

```dotenv
DGC_SAP_PROFIT_ENABLED=true
DGC_SAP_PROFIT_API_URL=https://116.63.221.181/post/sapincome
DGC_APP_KEY=<secret-store-injected-app-key>
DGC_APP_SECRET=<secret-store-injected-app-secret>
DGC_PAGE_SIZE=15000
DGC_MAX_RECORDS=100000
DGC_MAX_PAGE_BYTES=10485760
DGC_MAX_TOTAL_BYTES=67108864
DGC_TLS_SERVER_NAME=dgc.huaweicloud.com
DGC_TLS_PINNED_CERTIFICATE_SHA256=AF3850E5ACC206D12082BDD32E94AD4675F3AD7AB0AE23A247053DE9ED2883BF
DGC_SAP_PROFIT_FIELD_MAP={"client":"mandt","company_code":"bukrs","company_name":"companyname","fiscal_year":"gjahr","fiscal_period":"monat","ledger":"rldnr","line_number":"hs","line_item":"ztext","current_month_amount":"nmhsl","year_to_date_amount":"nyhsl"}
DGC_SAP_PROFIT_METRIC_MAP={"cumulative_profit":["利润总额","四、利润总额","四、利润总额（损失以“－”号填列）","四、利润总额(损失以\"-\"号填列)"],"fair_value_change":["公允价值变动收益","公允价值变动损益","公允价值变动收益（损失以“－”号填列）","公允价值变动收益(损失以\"-\"号填列)"],"cumulative_revenue":["一、营业总收入","营业收入"]}
DGC_SAP_PROFIT_LEDGER=0L
DGC_SAP_TRIAL_BALANCE_ENABLED=false
DGC_SAP_TRIAL_BALANCE_API_URL=https://116.63.221.181/fin/trial_balance
DGC_SAP_TRIAL_BALANCE_APP_KEY=<secret-store-injected-app-key>
DGC_SAP_TRIAL_BALANCE_APP_SECRET=<secret-store-injected-app-secret>
DGC_SAP_TRIAL_BALANCE_PAGE_SIZE=1000
DGC_SAP_ACCOUNT_BALANCE_ENABLED=false
DGC_SAP_ACCOUNT_BALANCE_API_URL=https://116.63.221.181/post/sapaccountbalance
DGC_SAP_ACCOUNT_BALANCE_APP_KEY=<secret-store-injected-app-key>
DGC_SAP_ACCOUNT_BALANCE_APP_SECRET=<secret-store-injected-app-secret>
DGC_SAP_ACCOUNT_BALANCE_PAGE_SIZE=15000
DGC_HESI_REIMBURSEMENT_ENABLED=false
DGC_HESI_REIMBURSEMENT_API_URL=https://116.63.221.181/post/hesimingxi
DGC_HESI_REIMBURSEMENT_APP_KEY=<secret-store-injected-app-key>
DGC_HESI_REIMBURSEMENT_APP_SECRET=<secret-store-injected-app-secret>
DGC_HESI_REIMBURSEMENT_PAGE_SIZE=100
DGC_HESI_REIMBURSEMENT_FIELD_MAP={"company_code":"company_code","approval_completed_at":"flow_end_date","expense_claim_code":"expense_code","expense_type_code":"fee_type_code","expense_type_amount":"fee_type_amount"}
DGC_HESI_INVOICE_ENABLED=false
DGC_HESI_INVOICE_API_URL=https://116.63.221.181/post/hesiinvoice
DGC_HESI_INVOICE_APP_KEY=<secret-store-injected-app-key>
DGC_HESI_INVOICE_APP_SECRET=<secret-store-injected-app-secret>
DGC_HESI_INVOICE_PAGE_SIZE=100
DGC_HESI_INVOICE_FIELD_MAP={"company_code":"company_code","expense_claim_code":"code","invoice_id":"invoice_id","expense_type_id":"feetypeid","expense_line_amount":"amount_standard_dec","invoice_approved_amount":"approve_amount_dec"}
DGC_SAP_DIVIDEND_DETAIL_ENABLED=false
DGC_SAP_DIVIDEND_DETAIL_API_URL=https://116.63.221.181/post/settlement_adjustment
DGC_SAP_DIVIDEND_DETAIL_APP_KEY=<secret-store-injected-app-key>
DGC_SAP_DIVIDEND_DETAIL_APP_SECRET=<secret-store-injected-app-secret>
DGC_SAP_DIVIDEND_DETAIL_PAGE_SIZE=15000
LARK_REFUND_WRITEBACK_ENABLED=false
LARK_REFUND_BASE_URL=https://hailiang.feishu.cn/base/A1Kwb4tkZaZdE2s3C2dcG49Fn2d
LARK_REFUND_API_BASE_URL=https://open.feishu.cn
LARK_REFUND_BASE_TOKEN=A1Kwb4tkZaZdE2s3C2dcG49Fn2d
LARK_REFUND_TABLE_ID=tbl4PCNdcl4BYzgZ
LARK_REFUND_COMPANY_CODE_FIELD_ID=fld5uBjB9R
LARK_REFUND_STATUS_FIELD_ID=fld4HLnqDk
LARK_REFUND_APP_ID=<secret-store-injected-app-id>
LARK_REFUND_APP_SECRET=<secret-store-injected-app-secret>
LARK_REFUND_TIMEOUT_SECONDS=30
LARK_REFUND_PAGE_SIZE=100
LARK_REFUND_MAX_RETRIES=3
LARK_REFUND_WORKER_CONCURRENCY=2
```

生产外部取数默认使用受控线程池和 Redis 防击穿缓存。并发上限、TTL、锁租约、重试策略、失败语义及监控指标见 [并行取数与 Redis 缓存运维](docs/operations/parallel-external-fetch.md)。真实接口失败不会回退到模拟数据或过期缓存，真实空结果按 `REAL/NO_DATA` 处理。

所得税退税状态回写配置默认关闭。目标已切换为飞书多维表“法人主体指标汇总表”，数据表为“法人主体所得税税负率&利润率等”。季度主数据按 `fldgeRGkKv`（所得税税率）、`fld3zvDri3`（递延所得税税率）、`fld70tcRFh`（可弥补亏损额合计）和 `fld5c2IX6N`（3年平均税负率）读取，税率使用 Base 返回的 0-1 小数口径；仅处理 `fld5uBjB9R`（公司代码）非空且唯一的记录，空代码记录排除。退税清单使用 `fld6bBYJeP`（2025年是否涉及退税）和 `fld5KnsfqZ`（2025年应退税金额），并只更新 `fld4HLnqDk`（是否已收到退税）为“已退税”。零条或多条精确匹配均失败，不猜测记录；远端已是“已退税”时按幂等成功处理。App ID/App Secret 必须由部署密钥管理注入，不得提交到仓库，且只向专用 `worker-income-tax-refund-writeback` 进程提供。

平台数据库和幂等 outbox 仍是主事实：扫描事务先持久化 `PENDING` 回写记录，专用 Worker 再获取租户令牌、查找 Base 记录并更新状态；网络或远端错误记录为 `FAILED` 并按有限次数重试。启用前须确认飞书应用已发布、拥有该多维表的记录读取/编辑权限，并被授权访问目标 Base。金额匹配仍按公司币种、`scale=2` 和 `ROUND_HALF_UP` 执行，该精度口径须经业务验收。

六项监测的飞书示警采用“平台写队列、人员逐行勾选推送”的方式。目标 Base 为 `A1Kwb4tkZaZdE2s3C2dcG49Fn2d`，队列表为“示警推送队列”（`tblUPRyqDLPTR4vv`），行级手动工作流为“示警明细手动推送”（`wkf2HRUWBZWhQyVV`）。新增或更新队列记录会把“推送”（`fldJU3PyLk`）明确设为未勾选，绝不触发消息；只有用户在具体明细行勾选“推送”时，Base 工作流才沿该行“法人主体”关联记录实时读取主表 `fld2f8VqpE`（业财）并发送。消息中的“填写整改信息”按钮打开当前队列记录，业财填写“修改的凭证号”（`fldCy6TW1d`）和“修改的会计年度”（`fldrzAPx9X`）后，内容直接保存在本条示警明细中。发送成功后，工作流把队列状态更新为“已提交”、记录提交时间并自动清空勾选。因此业财人员变更不需要修改平台配置，也不在平台代码、预览或队列中复制应用相关 `open_id`。

`backend/scripts/enqueue_feishu_alert_notifications.py` 每次都实时分页读取主表 `fld5uBjB9R`（公司代码）、`fld65JDObx`（公司名称）及记录 ID，仅处理公司代码非空且唯一的法人主体。试运行固定按六项能力顺序及最新报告中的公司顺序，每项最多选择前3家 `ALERT` 公司；每个“公司 × 能力”生成一条独立队列明细，包含检查结论、关键数值和具体示警内容。稳定的“推送唯一键”用于跳过已存在记录，重复运行不会重复建行。实际写入前会校验“示警推送队列”的19个字段、法人主体关联、业财查找字段、默认状态及反馈状态选项，防止 Base 字段漂移后静默生成残缺记录。两个整改反馈字段由业财填写，平台创建队列记录时保持为空，不会覆盖人工反馈。

默认命令只生成队列预览，不写 Base，更不会发送消息：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\enqueue_feishu_alert_notifications.py
```

确认预览后，显式追加 `--enqueue` 才写入待推送队列：

```powershell
.\.venv\Scripts\python.exe scripts\enqueue_feishu_alert_notifications.py --base-as user --enqueue
```

默认使用独立的 `tax-risk-notifier` Profile 和 bot 身份写队列；该应用只需目标 Base 的记录读写权限，不需要拥有动态业财名单的消息可用范围。当前应用尚未取得该 Base 的记录创建权限时，可显式使用 `--base-as user` 完成队列写入；这只写 Base，不会以用户身份发送消息。测试入队可加 `--test-push --max-items 3`，也可重复传入 `--company-code 3000 --company-code 3120 --company-code 3150` 限定法人主体。旧入口 `backend/scripts/send_feishu_alert_notifications.py` 仅作兼容包装，`--execute` 也只等价于 `--enqueue`，代码中已无直接消息 API 调用路径。预览默认写入 `artifacts/notifications/feishu-alert-queue-preview-latest.json`，不保存业财人员标识或任何飞书密钥。

真实全量检测完成后，平台会把所有 `ALERT` 公司及完整示警明细归档到同一 Base 的期间表：季度能力进入 `季度示警明细-YYYYQn`，月度能力进入 `月度示警明细-YYYY-MM`。归档仅接受 `source_mode=REAL` 且满足 `Base记录数 - 空公司代码数 = 实际检测公司数` 的全量报告；使用 `--max-companies` 的局部调试绝不归档。每次报告形成独立检测批次，`归档唯一键` 保证重复运行不重复建行；新批次只把本次覆盖能力的旧记录标为非当前，历史记录不删除，默认“当前示警”视图只显示最新结果。归档表通过“法人主体”关联主表，保存检查结论、关键数值及全部候选凭证明细，不触发任何飞书消息。纳税调增科目准确性检查按月归档，因此不会进入 `季度示警明细-YYYYQn`；归档表是审计合同，不应与人工推送及反馈队列表使用同一组字段。

`backend/scripts/run_real_full_validation.py` 默认在写出真实全量网页报告后归档六项能力；`backend/scripts/run_real_tax_adjustment_validation.py` 更新网页报告后只刷新纳税调增科目能力的月度归档。两条真实任务都会把该能力最多3家 `ALERT` 公司自动写入“示警推送队列”，记录初始状态为“待推送”、推送勾选为关闭，并由 Base 工作流完成业财解析及提交反馈；重复运行只确认已存在的推送唯一键。队列默认使用 `tax-risk-notifier` bot Profile，也可传 `--queue-base-as user`；仅排障时可用 `--skip-alert-queue` 跳过。紧急排障时可显式传 `--skip-alert-archive`，但正式全量任务不得使用。也可先单独预演，确认表名及行数后再写入：

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\archive_feishu_alert_results.py --base-as user
.\.venv\Scripts\python.exe scripts\archive_feishu_alert_results.py --base-as user --archive
```

利润表、科目余额表和汇总科目发生额接口不足以执行退税单条明细匹配。“汇算清缴相关科目明细”虽已提供凭证号、借贷标志、科目和摘要，但仍缺行项目唯一标识、过账日期和冲销标志。所得税费用科目范围已经业务表明确为 `6801010000/6801020000/6801030000`，平台只按代码匹配，不根据科目名称猜测；生产启用前仍必须取得完整 SAP 凭证行接口合同。由于尚未确认每月具体运行日，当前只允许通过幂等 API 或外部调度在 3-12 月触发，不内置未经确认的日历计划。

科目发生额表使用独立 AppKey/AppSecret。本地配置时在受 Git 忽略的 `infra/.env` 中填写密钥，在入库连接器完成前保持启用开关为 `false`，不要把密钥写入 `infra/env.example`。接口分页大小按合同默认值配置为 `1000`，`offsetValue` 从 `0` 开始并由后续连接器管理。

科目余额表接口地址为 `https://116.63.221.181/post/sapaccountbalance`，使用独立 AppKey/AppSecret。Body 参数为可选字符串 `company_code`、必填字符串 `fiscal_year/fiscal_period` 以及分页参数 `limitValue/offsetValue`；季度导入将月份规范为三位（如 `006`）。真实响应合同固定为 `account_code/account_name/closing_balance/company_code/company_name/credit_amount/debit_amount/fiscal_period/fiscal_year/input_tax_process_method/net_amount/opening_balance/sfkf` 共13个字段，并严格校验公司、年度和期间作用域。其他应付款暂估只取工作簿列示的十个 `224105` 科目：逐行期末余额大于等于0时取0，小于0时乘以-1，再精确求和；科目余额表查询成功但未返回任一目标科目时，生成有来源校验和的正零指标。SAP累计已计提递延所得税费用只取 `1811030000` 的期末余额原值；查询成功但未找到该科目时同样生成有来源校验和的正零指标。整个接口调用失败、响应不合法或未导入时仍按缺数据阻断。受控导入端点为 `POST /api/v1/ingest-batches/dgc-sap-account-balance`。

合思报销单明细接口地址为 `https://116.63.221.181/post/hesimingxi`，使用独立 AppKey/AppSecret。Body 参数 `company_code`（公司代码编码）和 `submit_date`（提交时间）可选，提供时不可为空；`limitValue` 和 `offsetValue` 必填并由连接器按每页 `100` 行从 `offsetValue=0` 连续翻页至末页，避免网关在大页请求时漏数。无票计算在分页完成后对完全相同的报销明细行去重，并把去重数量写入批次证据。真实字段合同使用 `expense_code`、`flow_end_date`、`fee_type_code` 和 `fee_type_amount`。

“合思报销单发票查询”接口地址为 `https://116.63.221.181/post/hesiinvoice`，使用独立 AppKey/AppSecret。该网关实际要求签名 GET 请求，`company_code`、`limitValue` 和 `offsetValue` 放入 Query；POST 会返回 `DLM.4313`。连接器同样固定使用不超过 `100` 行的小页并连续递增 offset，直至取完全部页面。无票计算要求 `invoice_id`，分页完成后按 `invoice_id` 去重；同一 ID 的字段载荷冲突时阻断，完全重复行只保留一条并写入批次证据。真实字段合同使用 `code`、`invoice_id`、`feetypeid`、`amount_standard_dec` 和 `approve_amount_dec`。

合思无票报销金额按公司、本年1月1日至季度末的审批完成时间聚合。合思报销明细排除以下费用类型编码：`CLF0101`、`CLF0102`、`CLF0103`、`CLF0117`、`CLF0118`、`CLF0119`、`CLF0120`、`CLF0126`、`CLF0130`、`CLF0131`、`F0507`、`F0508`、`F0605`、`F5409`、`F5724`、`F5725`、`F5809`、`F5811`、`F6309`。发票先通过 `hesiinvoice.code = hesimingxi.expense_code` 关联报销单，再在同一报销单内用 `amount_standard_dec = fee_type_amount` 及稳定的 `feetypeid` 唯一识别 `fee_type_code`；无法唯一识别时阻断计算。未审批单据不计入。系统按“报销单号 + 报销类型”分别汇总 `fee_type_amount` 与 `approve_amount_dec`，对每组计算 `max(报销金额 - 发票核准金额, 0)` 后汇总，不允许发票超额跨报销单或跨报销类型抵销无票金额。金额使用精确十进制。受控导入端点为 `POST /api/v1/ingest-batches/dgc-hesi-no-invoice`。

“发票明细”接口地址为 `https://116.63.221.181/post/writeoff`，使用独立 AppKey/AppSecret。Body 参数 `accounting_date`（入账日期）和 `comp`（公司代码）可选，提供时不可为空；`limitValue`和 `offsetValue` 必填并由连接器按 `15000` 的默认页大小自动管理。当前已完成独立配置、HTTPS 校验、签名分页和原始请求接线；因尚未提供返回字段合同，默认保持禁用，不做业务字段映射或入库。

“汇算清缴相关科目明细”数据表接口地址为 `https://116.63.221.181/post/settlement_adjustment`，使用独立 AppKey/AppSecret。平台按公司和年度发送 `company`、`fiscal_year`，不向远端发送 `fiscal_period`；`limitValue`、`offsetValue` 由连接器以 `15000` 为默认页大小自动管理。平台导入参数 `through_period` 仅用于在完整年度响应中按 3、6、9、12 月截取季度累计，防止历史季度重跑混入后续月份。发布合同包含以下 15 个字段：

| 字段 | 含义 |
| --- | --- |
| `company` | 公司代码 |
| `companyname` | 公司名称 |
| `fiscal_year` | 会计年度 |
| `fiscal_period` | 会计期间 |
| `voucher_no` | 凭证号 |
| `header_text` | 头摘要 |
| `detail_text` | 明细摘要 |
| `amount_ksl` | 金额 KSL |
| `gl_account` | 科目编码 |
| `account_name` | 科目名称 |
| `project_code` | 项目编码 |
| `project_name` | 项目名称 |
| `debit_credit_flag` | 借贷标志 |
| `group_currency` | 集团货币 |
| `original_system_doc_no` | 原始系统单据号 |

真实 scoped 查询已确认稳定返回其中 13 个字段，即省略作为查询作用域的 `company` 和展示字段 `companyname`。平台仅在请求明确指定公司时接受该精确 13 字段形态并回填公司作用域；若上游返回其他缺失或额外字段则直接拒绝。发布版完整 15 字段响应仍受支持，并会额外校验返回公司与查询公司一致。

累计分红取数仅保留 `gl_account` 精确等于 `6111010000`、`6111020000`、`6111030000`、`6111990000` 或 `6111150000`，且 `header_text`、`detail_text` 任一字段包含“分红”“股利”或“利润分配”的明细。累计分红金额按筛选后明细的 `sum(amount_ksl) * -1` 计算；`amount_ksl` 必须以 `Decimal` 解析和求和，并符合平台 `NUMERIC(38,12)` 金额范围，不得使用二进制浮点数，也不得再根据 `debit_credit_flag` 翻转符号，避免重复改变借贷方向。零结果统一规范为 `0`。

其他收益发生额复用同一“汇算清缴相关科目明细”响应，仅保留 `6112010000`、`6112020000`、`6112040000` 三个科目，并按 `sum(amount_ksl) * -1` 计算；季度截止、精确小数、币种和响应作用域校验与累计分红一致。该聚合变量已经接入解析层，但当前接口仍缺少退税单条匹配所需的行项目唯一标识、过账日期和冲销标志，因此不能替代完整退税凭证行合同。

所得税费用发生额同样复用“汇算清缴相关科目明细”，只保留 `6801010000`（当期所得税费用）、`6801020000`（递延所得税费用）、`6801030000`（以前年度所得税费用）。该变量按明细逐条计算：每条 `income_tax_expense_amount = amount_ksl * -1`，保留各自的凭证号、摘要、科目、期间和币种，不跨明细求和；单条 KSL 为0时规范为正零，零命中返回空明细。适配器先严格校验完整响应，再应用季度截止和科目过滤。该变量已接入解析层，但上述行项目唯一标识、过账日期和冲销标志缺口仍限制退税等额候选行的生产接线。

接口只接受 HTTPS；明文 HTTP 会返回 `DLM.4474`。正式客户端使用此前利润表、发生额表和合思联调相同的固定证书方式：发送凭据前先核对叶证书 SHA-256 指纹，匹配后将该证书作为唯一信任锚，并使用 `dgc.huaweicloud.com` SNI 完成主机名校验。客户端不关闭 TLS 校验、不跟随重定向、不读取环境代理，并复用连接。证书指纹变化时会在发送 AppKey 签名请求前失败。

受控导入端点为 `POST /api/v1/ingest-batches/dgc-sap-dividend-detail`。它先完成远端拉取、13/15 字段校验、季度截止过滤和币种校验，再创建批次并写入唯一的 `quarterly_metric/received_dividends` 行；零命中也写入有来源 checksum 和 scope 哈希的 `0`。外部部署仍默认关闭，只有在密钥、证书指纹、公司主数据和验收证据均已批准时才启用。

实时联调已确认可选 Body 参数为 `company_code/fiscal_period/fiscal_year/gl_account_code/input_tax_process_method/sfkf`，必填分页参数为 `limitValue/offsetValue`。成功响应使用 `errCode=DLM.0` 和 `data.data` 行数组，列为 `company_code/company_name/fiscal_year/fiscal_period/gl_account_code/gl_account_name/bank_center_code/bank_account_number/cost_center_code/cost_center_name/profit_center_code/profit_center_name/internal_order_code/internal_order_name/business_area_code/business_area_name/customer_code/customer_name/vendor_code/vendor_name/asset_code/asset_name/rstgr/rstgr_name/input_tax_process_method/sfkf/total_debit_amount/total_credit_amount`。

受控导入端点为 `POST /api/v1/ingest-batches/dgc-sap-trial-balance`。调用方按公司、年度和季度截止月份提交任务；平台只查询总账科目 `6801010000`，一次拉取该公司年度数据，不逐月重复请求。平台严格校验已发布的 28 个响应字段及公司、年度、科目作用域，再按截止月份将季度之前月份的 `total_debit_amount + total_credit_amount` 汇总为 `prior_quarter_current_tax`，将本季度三个月汇总为 `current_quarter_current_tax`。源金额符号原样参与加总，不按借贷方向二次翻转；空响应不补零，第一季度的“以前季度金额”在已有本季度源数据时记为有证据的 `0`。批次创建在远端拉取与完整校验之后，避免失败时写入半批数据。外部部署仍默认关闭。

利润表变量使用 `nyhsl` 本年累计金额，并精确匹配“`四、利润总额(损失以"-"号填列)`”“`公允价值变动收益(损失以"-"号填列)`”和“`一、营业总收入`”；同时保留真实接口返回的全角项目名称及历史“营业收入”别名，以兼容 SAP 展示格式差异。汇算清缴相关科目明细中的可选摘要、项目及原始单据字段允许源接口返回 `null`，并在过滤前规范为空字符串；凭证、科目、期间、金额和币种等业务身份字段仍禁止为空。

调用方提交受控批次元数据及 SAP 会计年度、期间和可选公司代码。平台将 `monat` 规范为两位并以对应月末作为批次期间，分页参数由连接器自动添加：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/ingest-batches/dgc-sap-profit \
  -H 'Content-Type: application/json' \
  -H 'X-Principal: <approved-principal>' \
  -H 'X-Principal-Signature: <signature>' \
  -d '{
    "source_batch_key": "sap-profit-2026-q1",
    "extraction_time": "2026-04-01T08:00:00Z",
    "gjahr": "2026",
    "monat": "03",
    "bukrs": "C001",
    "currency": "CNY",
    "amount_scale": 2
  }'
```

接口返回长表字段 `mandt/bukrs/companyname/gjahr/monat/rldnr/hs/ztext/nmhsl/nyhsl`。平台先按配置分类账精确过滤，再用 `ztext` 的配置标签映射 `cumulative_profit`、`fair_value_change`、`cumulative_revenue`；累计指标只读取 `nyhsl`，不读取本月金额 `nmhsl`。非目标项目被忽略，同一公司、期间、分类账和指标出现多行时整组拒绝，禁止静默求和。指定 `bukrs` 时，返回的其他公司也会被拒绝。

规范化的 `gjahr/monat/bukrs` 查询参数 SHA-256 会写入批次幂等元数据和 `payload_ref`，同一批次键不能改用另一组参数。响应中的财年和期间仍由批次校验确认，禁止用调用方期间替代缺失源字段。缺少某项指标时不补零，后续快照质量门禁会阻断计算。分红、所得税计提、暂估及合思无票报销等其他必需指标仍须由各自受控数据源导入。内网 HTTPS 证书必须包含接口 IP 的有效 SAN，并由容器信任链验证；不得通过关闭 TLS 校验规避证书问题。

## ✅ 验证

```bash
make test-backend
make test-backend-tiered
make test-web
make verify-governance
make security-check
make verify-migrations
make verify-release
make verify-capacity COMPANY_FIXTURE=126
make verify-rollback
make uat SNAPSHOT_SET=pilot-2026q2
```

`make test-backend-tiered` 对已接入的外部数据源执行二级接口测试。每个数据源独立判断：接口地址与完整密钥均存在时必须调用真实接口并标记为 `REAL`；密钥完全缺失时使用确定性模拟接口并标记为 `MOCK`。真实接口超时、签名失败、协议错误或字段校验失败会直接使测试失败，禁止静默回退到模拟数据；真实接口成功返回空列表时标记为 `NO_DATA`，仍保留 `REAL` 来源。唯一的业务解释例外是专用科目发生额查询：公司、年度和总账科目 `6801010000` 作用域完整且真实返回空列表时，表示该公司未计提，平台生成有来源 checksum 和查询范围哈希的以前季度、本季度金额 `0`；其他接口的空结果仍不得补零。判级不依赖生产启用开关，进程环境变量优先于本地 `infra/.env`。终端和 `artifacts/acceptance/backend-tiered.xml` 都会记录数据源、`REAL/MOCK` 模式、`DATA/NO_DATA` 状态及记录数，不记录密钥。

当前分级契约测试覆盖 SAP 利润表、SAP 科目发生额表、SAP 科目余额表、“汇算清缴相关科目明细”、合思报销单明细、合思报销单发票查询和发票明细。合思报销单明细的 `company_code` 与 `submit_date`、合思报销单发票查询的 `company_code` 仅在有值时发送，空字符串会在网络请求前被拒绝。

所有脚本使用失败即退出，并检查 JSON 或 JUnit 制品是否存在且满足阈值。季度外部技术栈浏览器测试需要先注入唯一的 105 家公司验收数据；未配置真实外部 E2E 时会明确标记为跳过，不能计入生产证据。

## 📈 监控与告警

```bash
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
curl -fsS http://127.0.0.1:8000/metrics
docker compose --env-file infra/.env -f infra/docker-compose.yml logs -f \
  api worker-quarterly worker-business-entertainment worker-monthly-semantic \
  worker-exports worker-income-tax-refund-writeback web
```

运维驾驶舱分别展示数据错误、技术失败和税务风险。`SnapshotSet.published_at` 是唯一数据就绪时间；公司成功提交完整结果后才写入 `company_output_ready_at`。数据或主数据阻断不得输出“无风险”，技术失败不得写入虚假输出就绪时间。

## 🔐 发布、升级与回滚

先运行治理、迁移、重放、容量、回滚和 UAT 门禁，再创建发布候选。规范清单锁定镜像摘要、Git 提交、迁移头、规则、提示词、模型适配器配置、科目字典、案例库和评估/重放报告摘要。

本地 `make verify-release` 只使用单次临时 Ed25519 密钥证明链路，制品标记为 `CI_EPHEMERAL_NOT_FOR_PRODUCTION`。试点和生产必须通过 OIDC 工作负载身份调用允许清单中的 KMS/HSM 密钥，并在下载制品后重新验签。完整步骤见[签名发布操作手册](infra/runbooks/release.md)。

升级数据库前必须备份并在隔离副本执行：

```bash
make verify-migrations
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini current
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini upgrade head
DATABASE_URL="$MIGRATION_DATABASE_URL" backend/.venv/bin/alembic -c backend/alembic.ini check
```

严禁在主数据库或唯一副本上直接执行降级。备份、恢复与回滚命令见[基础设施运维手册](infra/README.md)和[回滚与恢复操作手册](infra/runbooks/rollback.md)。

```bash
make verify-release
make verify-rollback
```

回滚会验证签名、排空或撤销任务、撤销导出、恢复隔离备份、演练迁移降级/重升、部署上一清单、比较校验和并重跑代表公司。相同批准输入可从检查点续跑，不重复恢复、部署或创建风险事项。

## 🧭 生产准入与推广

真实试点必须使用冻结公司快照，补齐集团税务、数据、安全和运维四类实名批准，并使用生产 KMS/HSM 签名。相关入口：

- [试点 UAT 操作手册](infra/runbooks/pilot-uat.md)
- [集团分批推广手册](infra/runbooks/group-rollout.md)
- [数据负责人检查清单](docs/operations/data-owner-checklist.md)
- [用户培训手册](docs/operations/user-training.md)
- [第四阶段验收评分卡](docs/operations/acceptance-scorecard.md)

## 📜 版本历史

第一至第四阶段的重要变更见 [CHANGELOG](CHANGELOG.md)。
