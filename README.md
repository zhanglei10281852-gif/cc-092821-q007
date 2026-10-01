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
- `app/germplasm/lineage.py` 管理不可变批次谱系账本：分装与合并、上下游追溯和重量守恒检查。
- `app/germplasm/viability.py` 管理检测规程、取样、重复计数、活力结果与复检日程。
- `app/germplasm/quality.py` 管理温湿度读数、偏离告警和种质发放审批。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 批次谱系

- 分装 `POST /api/germplasm/lineage/splits`：一次请求给出多个目标批次及重量，服务端在同一事务内
  按目标重量之和一次性扣减来源可用量（乐观版本锁守卫），并为每个目标建立子批次；总量超限、编号
  冲突、来源冻结等任一目标失败都会整体回滚。
- 合并 `POST /api/germplasm/lineage/merges`：只允许同资源、同处理条件、同收获年份、无质量冻结、
  最近一次活力结果处于同一风险档（未检测批次互不混并）的批次合并，任一输入失败整次回滚。
- 谱系事件（`lot_lineage_events`）与组件（`lot_lineage_components`）只追加，数据库触发器禁止
  UPDATE/DELETE；被谱系引用的历史批次（已领用、已用于检测、已耗尽）不能删除，仍可追溯。
- 业务键重试直接返回原谱系事件（响应中 `replayed=true`），不会重复扣减；版本冲突时扣减语句
  0 行命中，重量不可能增减两次。
- `GET /api/germplasm/lots/{lot_id}/lineage` 向上还原来源（`ancestors`）、向下汇总去向
  （`descendants`）；`GET /api/germplasm/lots/{lot_id}/conservation` 用谱系重量守恒和库存流水
  交叉核对，返回 `balanced` 与具体异常（事件失衡、可用量漂移、流水与谱系不对应等）。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。会话令牌只保存摘要，审计记录不保存明文密码或令牌。
