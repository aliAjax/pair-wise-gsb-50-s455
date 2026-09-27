# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚和证据完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8326
```

默认端口为`8326`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 涉刑移送

复核补税时，满足以下任一条件即构成涉刑移送门槛，系统提供独立移送台账，不能再按普通案件直接结案：

- 补税差额（涉案税额）超过 **50万元**；
- 存在隐瞒收入线索（创建字段`hidden_income`）；
- 存在销毁资料线索（创建字段`destroyed_records`，或`criminal_clues`文本）。

案件在普通状态机之外新增移送状态：

`transfer_pending`（建议待确认）→ `awaiting_dispatch`（已确认待发出）→ `awaiting_transfer`（已发出、待移送结果）；
公安退回补证进入 `supplementing`，重新核定税额后可再次移送，版本号递增；公安受理后为 `transfer_accepted`，之后才允许 `closed`。

移送动作（均走 actions 接口）：

| 动作 | 角色 | 说明 |
| --- | --- | --- |
| `transfer_propose` | inspector | 发起移送建议（自动校验门槛），不满足条件拒绝；退回补证后须先重新核定 |
| `transfer_confirm` | reviewer | 负责人确认，确认时生成全局唯一编号 `XS-YYYY-NNNNNN` |
| `transfer_reject` | reviewer | 驳回建议并记录意见，案件回到调查中 |
| `transfer_send` | reviewer | 移送公安发出，案件锁定在`awaiting_transfer`，**禁止提前结案** |
| `transfer_return` | reviewer | 公安退回补证，记录退回原因与补证要求 |
| `transfer_reassess` | inspector | 重新核定税额（重算差额/滞纳金/罚款），核定历史保留在payload |
| `transfer_result` | reviewer | 登记公安结果：`accepted`受理立案（带`police_case_no`）或`declined`不予立案（视同退回） |

再次移送在`transfer_ledger`台账中另存为新版本（round+1、新编号），历次版本、退回原因、重新核定金额均留存。

移送台账接口：

- `GET /api/transfers?record_id={id}&status={status}`：移送台账（可按案件/状态过滤）。
- `GET /api/transfers/{transfer_no}`：按唯一编号查移送记录。
- `GET /api/transfer-todos`：移送待办分组（待确认/待发出/待移送结果/待补证核定）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
