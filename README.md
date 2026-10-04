# 水库防汛调度与操作确认

根据库位、入库流量、下游警戒和施工限制生成复核授权的泄洪指令。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `src/recon_domain.py` / `src/recon_rules.py` / `src/recon_repository.py` / `src/recon_service.py`：汛期闸门遥测/手报对账子系统（来源仲裁、指令重算、关闭资格、补传续传）。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

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
- `GET /api/audit`

允许角色：duty_officer, chief_engineer, dispatcher, viewer。库位超过汛限或入库流量上升时提升紧迫度；授权前必须有复核记录，执行后仍要闭环现场反馈。

## 汛期闸门对账接口

把水情依据、闸门操作回执和关闭确认接成一条对账链。每笔数据都带`source`(device/manual)、`point`、`field_time`(现场时间)、`seq`(设备序列)和可选`batch_ref`(补传批次)。

- `POST /api/recon/points`：登记测点与开闸阈值`open_threshold`。
- `POST /api/recon/observations`：单笔水情上报（metric: water_level/inflow/downstream）。
- `POST /api/recon/batch`：设备恢复后批量补传，体`{batch_ref, point, observations:[...]}`；乱序按现场时间仲裁，同批次重试幂等（不重复落账、不重复追加审计），响应给出`resume_from_seq`续传进度。
- `POST /api/recon/receipts`：闸门操作回执（metric: gate_position/gate_flow，过流与到位，可迟到）。
- `POST /api/recon/closures`：关闭确认（position_value/flow_value），只确认与之自洽的已有回执。
- `POST /api/recon/commands/{id}/execute`：执行待执行指令。
- `GET  /api/recon/commands`、`GET /api/recon/closures`。
- `GET  /api/recon/pending`：留待核队列；`POST /api/recon/pending/{id}/resolve`（body `{"approve":true|false}`）由chief_engineer/duty_officer裁决。
- `GET  /api/recon/reconciliation?point=`：对账总览，含`diffs`(对账差异)、`recalc_chain`(指令重算来源链)、`resume_progress`(续传进度)、`canonical`(当前规范值)。

对账规则：

1. 同一测点、同一现场时间、同一指标冲突时**设备值优先**取代暂定人工值，但人工标明的`anomaly_note`合并保留。
2. 规范值一旦`confirmed`即锁定，后到数据（含设备补传）不覆盖，转入留待核；异常原因仍记录。
3. 水情依据或回执更新后：**未执行**指令按新结果置`invalidated`并生成新指令（`recalc_of_id`/`recalc_source`记录重算来源）；**已执行**记录永久保留，只重新核对关闭资格（qualified/pending/blocked 及原因）。
4. 补传失败后按测点已确认的最大`seq`继续；两个值班员同时提交同一测点，先到生效、后到返回409并留待核。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
