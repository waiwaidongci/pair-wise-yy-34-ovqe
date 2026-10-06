# 工伤事故调查与纠正措施

记录工伤经过、伤害、现场和证人，维护调查、纠正措施、验证与关闭流程，并把事故、措施记录与审计事件接成可恢复的关闭批次。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量与措施复验规则。
- `src/repository.py`：SQLite建表、事务、版本控制、审计链、关闭批次与检查点恢复。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败与关闭批次测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8311
```

默认端口为`8311`，首次启动自动建库并为旧库补充复验字段。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/close-batches`
- `POST /api/items/{id}/close-batches`，提交关闭批次，必须提交`expected_version`
- `POST /api/close-batches/{id}/sign`，安全经理签字
- `POST /api/close-batches/{id}/reconfirm`，作废后重新确认
- `POST /api/records/{id}/reinspect`，对措施做独立复验
- `GET /api/audit`

允许角色：reporter, investigator, safety_manager, viewer。严重度越高、伤害指数越大或未关闭措施越多，优先级越高；严重事故必须在4小时内启动调查。

## 关闭批次

提交关闭批次时冻结事故版本与全部措施依据（快照）。未关闭或未独立复验的措施会拦住推进：措施须为`closed`，且复验人（`reinspected_by`）不得与执行人（`executor`）相同。复验依据改动后，未完成批次作废并需重新确认；已关闭事故保留原快照。

两名调查员同时提交同一事故时，先落账的一方生效，后到者拿到版本冲突。审计写入失败后可从检查点恢复，按操作号接着办且不重复追加。旧数据中缺复验字段的措施升级为待补核（`pending_reinspection`），普通查询照旧。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
