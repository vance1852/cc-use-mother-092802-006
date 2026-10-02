# 追踪老产品去库存与处置责任链基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

包内同时实现了**老产品处置平台**（`disposal_*` 模块）：为金沙、景芝等主体清理渠道和自有库存中的老产品时，按批次保存库龄、包装状态、渠道位置、处置规则版本与责任方；计划审批后才生成提货、换包、折价、退仓或销毁步骤；跨主体交接双方确认且数量守恒；质量/标签风险只冻结受影响范围；销毁与退款事实不可被普通修订撤销。

## 目录

- `src/beverage_ops_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
  - `disposal_rules.py`：处置动作与包装状态字典、不可变规则版本的决策矩阵；
  - `disposal_models.py` / `disposal_storage.py`：处置平台数据对象与独立表结构；
  - `disposal_service.py`：批次、冻结、计划、步骤、交接、扫描、结算与管理视图；
  - `disposal_acceptance.py`：处置平台离线端到端验收；
- `tests/`：基础规则、事务边界、接口路由、处置全流程和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记经营主体、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

## 老产品处置平台

### 核心行为约定

- **批次档案**：每个批次保存产品、数量、库龄（`age_days`）、包装状态（`intact` / `scratched` / `label_old` / `damaged`）、位置（`channel` 或 `self_warehouse`）、渠道经销商、保管责任方，并永久快照登记时的处置规则版本。
- **规则版本不可变**：同一 `family + version` 一经登记，决策矩阵（包装状态 → 提货/换包/折价/退仓/销毁）与损益口径即冻结；相同内容重复登记返回幂等回执，内容不同则冲突。
- **先审批后执行**：计划只有 `draft → submitted → approved` 之后才生成步骤；审批人不能是创建人；换包自动拆成「提货到换包站 + 站内换包回仓」两个步骤。计划行在审批时再次校验冻结数量与其他在执行计划的占用，防止超卖。
- **数量守恒**：批次原始量 = 各位置在库（含 `in_transit`）+ 已销毁 + 已折价售出，任何交接落账前后都保持平衡，可通过 `GET /disposal/conservation` 核对。
- **双方确认交接**：交接事件先 `pending`，发出方确认后货入 `in_transit`（步骤 `awaiting_counterparty`），接收方确认后才到达目的位置；两方确认缺一不可。已起运（在途）不能拒收，只能发起反向交接。同一主体（如自有仓销毁）自动完成双方确认。
- **范围冻结**：风险可按 `batch`（整批）、`package_group`（包装组，需 `scope_ref`）或 `quantity`（数量）冻结，只阻断受影响部分；冻结期间在途货物不受影响；解除后恢复执行。
- **终态不可撤销**：销毁经双方见证确认、折价在「销售扫描 + 退款/回款结算 + 渠道方确认」后扣减货物，步骤置 `irreversibly_closed`。结算事实一个步骤只能登记一次；终态步骤拒绝新交接，改版也不能让已销毁/已退款数量回来。
- **确定性的边界情况**：
  - *晚到回执*：步骤已完成、已随改版作废或超过 `due_at` 后登记的扫描标记 `late=true`，只留证据、不重开步骤、不动账；
  - *重复扫描*：同一步骤 + 扫描类型 + 证据编号的再次扫描标记 `duplicate_of`，不产生任何效果；
  - *重复确认*：对已完成交接再次确认返回幂等回执；
  - *计划改版*：旧版置 `superseded`，其未开始步骤置 `superseded`，在途/待确认步骤必须先结清；已完成步骤、销毁与结算事实全部保留，新版为草稿并带递增 `revision`。

### 主要接口（均以 `/disposal` 开头）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/disposal/rulesets` | 登记不可变规则版本 |
| POST | `/disposal/batches` | 批次建档（库龄/包装/位置/责任方/规则版本） |
| GET | `/disposal/batches` | 按 `product_id` 或 `dealer_id` 列批次 |
| GET | `/disposal/batch?batch_id=` | 批次详情（含冻结状态） |
| POST | `/disposal/freezes` / `/disposal/freeze-lifts` | 发起 / 解除范围冻结 |
| GET | `/disposal/freezes?batch_id=` | 批次冻结记录 |
| POST | `/disposal/plans` `/plan-updates` `/plan-submissions` `/plan-approvals` `/plan-rejections` `/plan-revisions` | 计划全生命周期；审批后生成步骤 |
| GET | `/disposal/plans` `/plan` `/steps` | 计划与步骤查询 |
| POST | `/disposal/handovers` `/handover-confirmations` `/handover-rejections` | 跨主体交接发起、双方确认、拒收 |
| POST | `/disposal/scans` | 现场扫描回执（返回 `duplicate` / `late`） |
| POST | `/disposal/settlements` | 折价退款/回款事实（一次写死） |
| POST | `/disposal/discount-confirmations` | 渠道方确认折价终态 |
| GET | `/disposal/pending-actions` | 未完成动作，可按 `product_id` / `dealer_id` 过滤 |
| GET | `/disposal/overdue-actions` | 责任逾期动作（超过步骤 `due_at`） |
| GET | `/disposal/loss-basis` | 损益依据（结果类型、数量、规则口径、结算时间） |
| GET | `/disposal/custody-chain?batch_id=` | 完整保管链（登记→冻结→计划→交接→扫描→结算→守恒） |
| GET | `/disposal/conservation?batch_id=` | 批次数量守恒核对 |

所有写接口都需要 `request_id` 幂等键和 `X-Actor-Id`，操作结果进入与基础模块同一条哈希审计链。

### 处置平台离线验收

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.disposal_acceptance
```

该命令在临时库中走通规则登记、四种包装状态批次、范围冻结、审批生成五类步骤、双方确认交接、重复/晚到扫描、折价结算终态与计划改版，核对全部批次数量守恒和审计链，成功时输出 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。
