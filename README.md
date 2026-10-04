# 水库防汛调度与操作确认

根据库位、入库流量、下游警戒和施工限制生成复核授权的泄洪指令，并把水情依据、闸门操作回执和关闭确认接成对账流程。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和对账规则。
- `src/repository.py`：SQLite建表、事务、版本控制、读数冲突裁决和审计链。
- `src/service.py`：权限检查、用例编排、并发控制、失效重算、关闭资格核对和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败和对账测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8315
```

默认端口为`8315`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/readings`：水情读数列表（可按`point`过滤）
- `POST /api/readings`：提交一笔读数（来源`device`/`manual`、测点、现场时间、值、异常原因）
- `POST /api/readings/{id}/confirm`：首席工程师锁定待核读数为已确认
- `GET /api/readings/reconciliation`：对账视图，按测点+现场时间汇总设备值与人工值、差异和异常原因
- `POST /api/backfills`：设备恢复后批量补传乱序读数，可续传、重试不重复追加审计
- `GET /api/backfills`：补传批次列表与续传进度
- `GET /api/audit`

允许角色：duty_officer, chief_engineer, dispatcher, viewer。库位超过汛限或入库流量上升时提升紧迫度；授权前必须有复核记录，执行后仍要闭环现场反馈。

## 对账流程

- **每笔读数带来源、测点和现场时间**：`source`区分遥测设备(`device`)与人工手报(`manual`)，`point`为测点，`observed_at`为现场时间。
- **同一测点同一时间冲突时设备值优先**：设备读数自动确认(`confirmed`)，人工读数置为留待核(`held`)；人工标明的异常原因(`reason`)始终保留。
- **已确认值不被后到数据盖掉**：已确认(`confirmed`)读数锁定，后到数据一律留待核(`held`)。
- **先到生效，后到留待核**：两个值班员提交同一测点同一时间，先到的人工读数生效(`pending`)，后到的留待核(`held`)。
- **依据或回执更新后失效重算**：未执行指令按新结果重算优先级/期限并置失效标记(`invalidated`)，授权后清除；已执行记录保留并重新核对关闭资格(`closure_recheck`)。
- **补传可续传**：批次失败后从已确认测点继续(`resume_index`)，已处理读数不重复落库、不重复追加审计；列表和审计展示对账差异、重算来源和续传进度。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
