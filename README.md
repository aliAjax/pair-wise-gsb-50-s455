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
- `GET /api/records/{id}/transfers`：某案件的移送版本记录（再次移送另存版本）。
- `GET /api/transfers`：涉刑移送台账，可带`state`、`record_id`和`limit`参数。
- `GET /api/transfers/todo`：移送待办（待确认编号、待发出、待移送结果、待补证重核），含退回原因。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色包括`inspector`、`reviewer`、`taxpayer_rep`、`leader`和`admin`。

## 涉刑移送

复核后涉案税额超过50万元，或存在隐瞒收入、销毁资料等线索的案件，不能按普通案件结案（`close`被拒绝），必须走涉刑移送流程：

1. `propose_transfer`（reviewer）：提出移送建议，记录线索和移送理由，案件进入`transfer_proposed`。
2. `confirm_transfer`（leader）：负责人确认唯一移送编号（缺省自动生成`税移字〔YYYY〕NNNN号`，也可指定，重复编号被拒绝），进入`transfer_confirmed`。
3. `send_transfer`（reviewer）：移送文书发出，案件停在`transfer_pending_result`，此期间不能提前结案。
4. `resolve_transfer`（reviewer）：登记公安结果。`accepted`直接移送结案；`returned`必须记录退回原因，进入`transfer_returned`。
5. `reassess`（inspector）：退回补证后重新核定税额，重算补税、滞纳金和处罚，案件回到`reviewed`，可再次移送（台账另存新版本）；若重核后不再满足移送条件，可正常结案。

每次移送在`transfers`台账中存为一个版本，移送编号、线索、退回原因、经手人和时间全程留痕，审计时间线同步记录移送编号。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
