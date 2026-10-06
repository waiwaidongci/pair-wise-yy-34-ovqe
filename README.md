# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、独立复验与可恢复的关闭批次流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误（含可恢复错误）、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量与措施依据快照。
- `src/repository.py`：SQLite建表/迁移、事务、版本控制、关闭批次、审计outbox与哈希链。
- `src/service.py`：权限检查、用例编排、并发控制、检查点恢复和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、关闭批次、并发恢复和历史数据迁移测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库；旧库启动时自动加列迁移。使用`X-Actor`和`X-Role`请求头传递身份。

## 关闭批次语义（事故关闭）

关闭不再是“拿旧措施直接推进”，而是一个可恢复的批次：

1. 事故进入`verification`后，调查员调用提交接口创建**关闭批次**。
2. 提交时**冻结事故版本与全部措施依据**（措施状态、执行人、复验人、复验依据的快照+SHA-256）；提交本身占用一个新版本号。
3. 推进前逐措施检查：未关闭、未独立复验、复验人与执行人相同、或历史数据缺复验字段（待补核）都会**拦住推进**（409）。
4. 安全经理凭冻结版本确认，事务内一次性关闭事故并追加确认审计。
5. 待确认期间任一措施的执行/复验依据改动，**未完成批次自动作废**并留下审计检查点，必须重新提交；已关闭事故保留原快照，拒绝任何依据改动。
6. 两名调查员并发提交同一事故：先落账者生效，后到者收到409及`current_version`，刷新后重新确认。
7. 审计写入失败不影响业务落账：事件进入`closure_ops`检查点，恢复接口**按操作号(seq)续办**；以`op_key`幂等，重复恢复不会重复追加，哈希链保持有效。
8. 历史措施缺复验字段时**读侧升级为`pending_supplement`待补核**，不静默改写数据，普通记录/普通查询照旧。

## 主要接口

- `GET /health`
- `GET /api/items` / `POST /api/items` / `GET /api/items/{id}`
- `POST /api/items/{id}/records`（措施传 `kind=measure`、`is_measure=true`，可选 `executed_by`）
- `GET /api/items/{id}/records`
- `POST /api/items/{id}/transition`（必须提交`expected_version`）
- `POST /api/records/{id}/execute`：登记/完成措施执行（investigator）
- `POST /api/records/{id}/verify`：独立复验，`verify_ref`必填，复验人不得等于执行人
- `POST /api/items/{id}/closure-batches`：提交关闭批次，body含`expected_version`
- `GET /api/closure-batches?item_id=&status=` / `GET /api/closure-batches/{id}`
- `POST /api/closure-batches/{id}/confirm`：安全经理确认关闭
- `POST /api/closure-batches/{id}/recover`：按操作号恢复该批次审计
- `POST /api/closure-batches/recover`：恢复全部待办审计检查点
- `GET /api/audit`

错误码：422校验、404不存在、403无权、409冲突/拦截（`detail`带`current_version`、`blockers`等）、503业务已落账但审计待恢复。

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
