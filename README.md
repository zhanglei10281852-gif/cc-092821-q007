# 种质资源入库与活力复检服务

本项目是面向种质资源库的 Python 后端服务，用于登记采集或引进材料、建立种子批次、管理低温库位和容器移动、执行发芽活力检测、生成复检日程并处理环境与质量告警。档案、库存、检测和发放审批都保存在本地 SQLite 中，关键写入带版本或幂等键，适合在单个 Linux 应用容器内运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `data/germplasm.db`，也可以通过 `GERMPLASM_DATABASE_PATH` 指向其他 `.db`、`.sqlite` 或 `.sqlite3` 文件。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。种质业务接口统一位于 `/api/germplasm`。

## 测试与构建检查

```bash
python -m pytest
python -m compileall -q app tests
```

下面两条命令分别检查 HTTP 入口和完整的入库演示链路：

```bash
python -m app.cli smoke
python -m app.cli demo
```

## 业务边界

- `app/germplasm/accessions.py` 管理来源、资源档案、护照信息与接收状态。
- `app/germplasm/inventory.py` 管理批次、库位容量、容器摆放、移动、领用和冻结。
- `app/germplasm/lineage.py` 管理不可变批次谱系账本：分装、合并、消耗登记、上下溯源与守恒检查。
- `app/germplasm/viability.py` 管理检测规程、取样、重复计数、活力结果与复检日程。
- `app/germplasm/quality.py` 管理温湿度读数、偏离告警和种质发放审批。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 批次谱系账本

为证明每一克种子的来路并阻止跨资源误合并，谱系账本提供独立于普通库存流水的只追加记录：

- `lineage_events` 记录分装、合并以及取样/领用/报废事件，以业务键唯一去重；`lineage_ledger_entries` 按事件序号记录每个批次在事件中的来源（source）、产品（product）或消耗（consumption）行及当时余额。两张表均由数据库触发器禁止 UPDATE/DELETE；批次对外键全部为 RESTRICT，已领用或用于检测的历史批次只能追溯、不能删除。
- `POST /api/germplasm/lineage/splits` 按多个目标重量一次性扣减来源可用量并建立子批次；`POST /api/germplasm/lineage/merges` 在建立合并批次前校验资源、处理条件、收获年份、未解除冻结以及最近活力结果（高/中/低三档，低于 50% 拒绝，跨档位超过一级拒绝）。任一目标或来源失败，整次操作通过保存点回滚。
- 扣减使用带版本号与余额条件的单条 UPDATE，并运行在 IMMEDIATE 事务中：版本冲突或并发争抢余量时重量不会变化，业务键重试直接返回原谱系，不会二次增减。
- `GET /api/germplasm/lots/{lot_id}/lineage` 向上还原来源、向下汇总去向（跨合并按投入量归属、跨分装沿路径取较小值，钻石结构按路径汇总）；`GET /api/germplasm/lineage/conservation` 逐批次核对账面可用重量、谱系收支与普通库存流水，并校验分装/合并事件的来源与产品重量是否守恒，指出异常流水。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。分装、合并和消耗额外写入只追加的批次谱系账本，业务键重试返回原谱系，扣减带版本与余额条件保证不重复增减。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
