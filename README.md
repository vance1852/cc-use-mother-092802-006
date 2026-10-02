# 追踪老产品去库存与处置责任链基础服务

本项目提供酒类生产、品牌和渠道团队共享的后台基础能力，负责经营主体、生产经营站点、操作者与结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务和哈希串联审计。领域项目可以在这些稳定边界上增加独立的业务状态、规则与接口。

`beverage_ops_foundation.disposal` 子包在此基础上实现**老产品处置平台**：金沙、景芝等品牌在渠道和自有库存中清理老产品时，为每个批次保存库龄、包装状态、渠道位置、处置规则版本和责任方，计划经独立审批后生成可执行步骤，全程记录货物最终去向与保管链。

## 老产品处置平台

### 批次台账

- 批次登记保存：产品、数量、单位成本、生产日期/入库日期（自动计算库龄）、包装状态（完好/褪色/破损）、库位类型（自有/渠道/仓库/销毁点）、所在主体与库位、经销商、责任方、引用的不可变规则版本。
- 规则以不可变版本发布（`publish_rule`），批次永久锁定登记时的规则版本；新规则只影响之后登记的批次。
- 规则引擎按库龄、包装、渠道位置给出确定建议：正常销售、换标、折价、退仓、销毁；计划行决策必须与规则判定一致（销毁始终为兜底允许项）。

### 冻结范围

- 发现质量或标签风险时，按**数量**冻结，可多次、按不同范围冻结（如仅 LOT-A），只冻结受影响范围，不波及同批次其他货物。
- 在途货物不受新冻结影响；冻结数量立即从可计划数量中扣减，解冻需登记原因。

### 计划、审批与改版

- 处置计划只保存批次行（决策、数量、折价单价、目标主体、期限），经与起草人不同的审批角色通过后，才为每行生成一个可执行步骤：提货 `pickup`、换包 `relabel`、折价 `discount`、退仓 `return_to_warehouse`、销毁 `destroy`。
- 每行保存批次快照（库龄、包装、位置、责任方、规则版本），审批时复核数量，起草后新增的冻结或竞争占用会阻断审批。
- **改版有确定行为**：旧版本整体作废、未发运步骤标记作废，新版本重新走草案→审批；只要存在在途或已完成步骤（销毁事实、退款事实），普通改版一律被拒绝。

### 执行、交接与数量守恒

- 跨主体步骤（提货、折价、退仓）必须由发货方 `dispatch` 与收货方 `receipt` **双方分别确认**，按主体校验权限；收货先于发货的回执拒绝入账。
- 收货时源批次台账扣减、新保管批次（带父批次血缘）同事务入账，全局可通过 `GET /disposal/conservation` 校验无负库存、每个完成步骤出入数量相等、交接均为双方确认。
- 换包在本方内部执行，产出标记为非老产品（`is_legacy=false`）的新批次。
- 销毁必须提交销毁凭证号、见证人与方式；销毁台账与凭证不可通过改版撤销。
- 退款只追加到已完成销售步骤上，同样不可被改版撤销。

### 晚到回执、重复扫描

- 同一扫描码重复上报到同一步骤同一角色：确定性忽略（`duplicate_scan=true, result=ignored`），不产生任何变动。
- 扫描码被用于其他交接动作：返回冲突，杜绝串单。
- 落在已改版作废步骤上的晚到回执：确定性拒绝并提示重新审批；尚未发货的收货回执拒绝入账。
- 整单重试（相同 `request_id`）优先回放原始结果。

### 管理 API

| 接口 | 用途 |
| --- | --- |
| `GET /disposal/batches?product_id=&distributor_id=` | 按产品/经销商查看批次与库龄、冻结、在途、可用数量 |
| `GET /disposal/open-actions?product_id=&distributor_id=&overdue_only=` | 未完成步骤、责任逾期（含逾期天数）、待审批草案 |
| `GET /disposal/profit-loss?product_id=&distributor_id=` | 损益依据：销毁成本损失、折价/提货收入、退款、凭证来源 |
| `GET /disposal/custody-chain/{batch_id}` | 完整保管链：批次血缘、台账出入、双方交接确认、冻结记录 |
| `GET /disposal/conservation` | 数量守恒与销毁凭证完整性核对 |

写接口包括 `POST /disposal/rules`、`/disposal/batches`、`/disposal/freezes`、`/disposal/freezes/release`、`/disposal/plans`、`/disposal/plans/approve`、`/disposal/plans/revise`、`/disposal/steps/handoff`、`/disposal/steps/execute`、`/disposal/steps/refund`。

## 目录

- `src/beverage_ops_foundation/`：基础领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `src/beverage_ops_foundation/disposal/`：老产品处置域的模型、表结构、领域服务与离线验收；
- `tests/`：基础规则、事务边界、接口路由、处置域规则/冻结/计划/交接/查询和端到端验收测试。

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
PYTHONPATH=src python3 -m beverage_ops_foundation.disposal.acceptance
```

验收命令在临时 SQLite 数据库中完成登记与处置全流程，核对幂等回执、范围冻结、双方交接、数量守恒、改版阻断、销毁/退款不可撤销、损益与保管链以及审计链；成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m beverage_ops_foundation.api --database beverage_ops.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
