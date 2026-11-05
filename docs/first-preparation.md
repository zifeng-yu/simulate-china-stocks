# 数据准备与独立数据服务

当前进度、真实数据截止、用途就绪与最新回归只在[实施状态](implementation-status.md)维护；机器读取[当前项目状态](../logs/project-current-state.json)。本文保留本层契约及明确日期的验收事实，日期日志不随当前状态刷新。

数据层已实现首次/月末选池、日常增量、标准化读取、交易日历、权益与策略输入、来源修订和不可变快照。它可单独运行，不要求账户或模型配置，不调用 LLM，不修改账本。全部程序与产物位于本仓库根目录 `simulate-china-stocks/`。

当前D01—D20已实现并验收，包括账本所需的按权益动作检查、跨历史窗口待办刷新、税基来源/未知契约和公开只读证据归档。完整方案与完成范围见[数据层清单](data-layer-backlog.md)和[账本附件](ledger-design.txt)。10月30日SI01全项目1519项测试及Ruff通过，10月31日CSS修正相关130项测试及Ruff通过；其回归覆盖模型及数据/账本/执行契约。模型SM01、执行SX01已验收；SI01代码已实现、自动化通过，用户浏览器验收已完成，M01—M06及V01—V06均PASS，C5/C6/C8通过，SI01界面验收COMPLETE，当前结果见[组件记录](../logs/interface-si01-2026-10-30-verification.json)，实际数据截止已为10月30日；本轮当前程序/数据验收见文首。

## 两个采集入口，共用一份数据

### 权益缓存时间校验与来源恢复（2026-10-29，已修正并验收）

命令2处理交易日D时，权益来源原件的取得时间须不早于D日北京时间18:10，才能作为本轮收盘后刷新成功的证明。同一自然日上午为前一交易日采集的原件，不能仅凭“今天取得”复用成D日的新覆盖。待结权益即使查询的是更早年份，也按本轮D的刷新时点检查；同一晚满足时点的原件可以复用。

覆盖记录的生成时间不能代替来源原件的取得时间。只读核验须检查覆盖依赖，旧原件不足时保持等待；刷新失败保存原因及重试时间，不能借旧覆盖报本轮刷新成功。上述校验已并入现行程序，不改变命令1/月末、命令2/每日18:10的调用方式，不重下载已合格历史行情。SL开发前1519项统一回归及此前真实续跑已通过；10月29日当时采集任务为COMPLETE，失败与修正经过保留在[调整记录](architecture-change-log.md)。

主源连续网络故障时，按来源执行短期冷却：同一来源连续3次网络错误后冷却60秒，期间直接保留该源补齐任务并继续其他来源；到期允许再次探测，成功后清零。来源未发布、格式错误不混同网络故障；服务端Retry-After和单请求冷却继续遵守，冷却不算请求成功。主动中断保存PAUSED及中断原因，后续仍用同一命令恢复；取得唯一写锁后也将遗留RUNNING标记为PAUSED，保留原始时间，避免已退出的采集显示仍在运行。此项降低源全面故障时逐股重复等待的影响，不保证外部来源必然恢复。

`prepare-data` 负责首次建立或月末更新候选池，复用已有的 20 日成交额选池和 121 日历史准备程序。成功结果导入共享市场数据索引，供其他层统一读取。

`data-sync` 负责给已选股票与必要证券持续补缺，使用同一数据索引、标准化规则和读取服务。调用者提供候选、持仓、挂单、待处理权益的代码；普通命令行未传`--codes`时读取已生效候选池。显式提供`--pending-event-ranges`历史权益范围文件时，不自动加载候选池，需要同时维护的候选代码由调用者明确传入。处理优先级为持仓、挂单、待处理权益、候选，去重后维护；出池股票只要仍在这些必要清单中，就不会停止取数。数据层不读取账户来猜测需要哪些股票。

两者不是两套交易系统。首次准备保留的 manifest 和文件用于选池续跑与来源追溯，共享索引才是其他业务层的读取入口。

## 日常与月末采集日程（当前约定）

以下时间均为北京时间（Asia/Shanghai）。**命令1是 `prepare-data`，命令2是 `data-sync`。每个交易日18:10后执行命令2；每月最后一个交易日18:10后，另执行命令1更新候选池。** 自动业务同时受17:00—19:00避让门禁约束，日终任务在19:00后首次检查或开机实际执行；独立人工命令仍遵守18:10门槛。首次准备已完成，不需要每天重新选池。

| 时间点 | 执行任务 | 作用 |
|---|---|---|
| 普通交易日D，18:10到期，自动19:00后首次检查/开机 | 命令2 `data-sync --as-of D --download` | 维护当前生效候选池及调用方传入的持仓、未结订单、未付权益证券；补齐历史缺口、D日行情、权益和指数 |
| 当月最后一个交易日D，18:10到期，自动19:00后首次检查/开机 | 当天命令2照常执行，另执行命令1 `prepare-data --as-of D --download` | 完成当天数据维护，同时准备下一交易日N生效的后继候选池；月末换池不替代当天增量任务 |
| 非交易日 | 无需例行生成一个新的交易日采集任务 | 可以恢复之前未完成的任务、补取缺口；不把自然日当交易日 |

“月末”指**当月最后一个交易日**，不一定是自然月最后一天。程序通过来源交易日历的 `is_month_end` 判断：D是交易日，且下一交易日已属于下个月。日历未知时保持 `WAITING_CALENDAR`，先补日历，不能凭周一至周五或固定30/31号猜测。18:10是允许处理当日数据的门槛，不保证来源此刻已经发布完整数据；自动业务在19:00后检查来源并执行。

月末当晚的调用约定如下，现行workflow按此顺序组织；独立手工调用时也须顺序执行：

1. 先执行命令2，维护D日生效的股票池和必要证券，并补齐已公布日历。某股或某源失败单独留待办，不因此删除旧池。
2. 确认D为当月最后一个交易日后，执行命令1。后继池从下一交易日N生效；选池发布失败保留原池。若新池已冻结/发布而历史或权益补缺未完，保持该池与原待办，按用途等待及续跑，不回退重选或宣称全部完成。
3. **新池次日生效，不等于月末当天命令2会自动选中新池。** 当前 `data-sync --as-of D` 不带 `--codes` 时只读取D日已经生效的池。为N制定计划前，如新池成员还缺规范权益或策略所需输入，应由调用方从命令1结果取得新池代码，再通过命令2的 `--codes` 或 `DataRequest.candidates` 补齐，行情截止仍为D，并继续传入持仓/订单/权益必要证券。已有合格缓存复用，不取尚未发生的N日行情。这是同一命令2的补缺调用，不增加另一套采集程序，也不承诺月末固定只调用各命令一次。
本次第三步已显式传入11月2日新池全部100只，as-of仍为2026-10-30；94只已齐输入复用，只补SH600887、SH603236、SH603533、SH600938、SZ002714、SH601127共12次权益来源请求，无价格/历史请求。此前命令1新批次重新请求100股121日历史属于14:59准备阶段，旧历史保留；两阶段不可混称。

4. 各任务完成后按用途查询就绪状态；已记录的来源矛盾继续隔离，不能只凭退出码或候选池切换宣布可交易。

在本仓库根目录，手动调用形式如下；`YYYY-MM-DD`替换为要处理的、已到采集时点的交易日，不是直接运行的字面日期：

```sh
# 命令2：每个交易日的常规增量。
./.venv/bin/python -m china_stock_sim.market data-sync --as-of YYYY-MM-DD --download

# 命令1：仅首次准备或最后一个交易日的月末换池。
./.venv/bin/python -m china_stock_sim.market prepare-data --as-of YYYY-MM-DD --download
```

两条采集命令顺序运行，不并发写市场索引。来源暂未发布、网络失败或预算耗尽时保存任务状态，按允许重试时间再次调用同一任务补缺；来源内容矛盾不要求立即反复全量重跑。机器关机/睡眠时不执行，开机后恢复漏掉的交易日和月末待办，保留实际取得时间，不能补造过去盘前计划。持仓、历史持仓及未结事项由现行WorkflowData通过账本公开接口合并传入，不能仅因它退出候选池就停止维护。

**上述顺序已接入现行workflow，生产自动调度的实际启用和安装状态见唯一当前入口。** 定时触发、冷却与重启恢复、月末任务编排见[workflow-design.txt](workflow-design.txt)，WF验收进度见唯一当前入口；正式运行无需用户每日输入命令，也不依赖LLM监控。请求/任务状态保存在 `data/state/market.sqlite3` 的 `requests`、`jobs`，首次/月末准备进度还保存在 `data/state/preparation/`；通过 `data-status` 查看已保存进度。

## 首次与月末的执行顺序

1. 读取 `configs/preparation.toml`，校验截止日已到北京时间 18:10。取得来源交易日历，取最近 121 个交易日；非交易日落到此前最近交易日，不按周一至周五猜测。
2. 取得证券快照，筛选沪深主板普通股票。当前快照记录真实取得时间，不包装成过去当时已知的证券身份。
3. 取得最近 20 个交易日的批量行情，按平均成交额排序，冻结最多 100 只候选。窗口缺日、量额无效、ST、当前停牌、上市时间不足等原因单独记录；坏股被排除，其他有效股票可以继续。
4. 对冻结池逐只取得 121 日不复权行情，校验代码、日期、唯一键、价格范围、量额、交易状态，并核对与批量行情的重叠部分。量统一为股、额统一为元；缺行不解释为停牌。
5. 保存复权因子及按除权年份查询的分红线索。原始线索保持 `CLUES_ONLY`，不能直接作为账户现金、到账股份或完整执行资格。
6. 保存准备产物，并将已通过校验的行情、日历、证券、池版本与来源引用导入共享索引。权益与各用途完整性继续由统一服务判定。

首次没有生效池时可以准备新池。已有池时，普通运行只在已确认的月末准备后继池，后继池在已知的下一交易日生效；非月末保留旧池。月末或下一交易日日历未知时返回 `WAITING_CALENDAR`，先用增量采集补齐已发布日历。新准备失败保持旧池，`--force` 可显式准备后继池但不删除历史版本。

## 独立命令

在仓库根目录运行。日期为示例，真正采集使用已经到采集时点的日期：

```sh
# 数据环境检查：不登录来源、不联网、不检查账户或模型。
./.venv/bin/python -m china_stock_sim.market data-doctor

# 首次/月末准备，默认检查缓存；允许保存进度但不联网。
./.venv/bin/python -m china_stock_sim.market prepare-data --as-of 2026-10-27

# 显式下载，可分段运行同一条命令续跑。
./.venv/bin/python -m china_stock_sim.market prepare-data \
  --as-of 2026-10-27 --download --max-requests 30

# 增量采集指定候选与持仓，默认还采集权益和三个指数。
./.venv/bin/python -m china_stock_sim.market data-sync \
  --as-of 2026-10-27 --codes SH600000 --holdings SZ000001 --download

# 只读查看已保存进度。
./.venv/bin/python -m china_stock_sim.market data-status
```

`data-sync` 不带 `--download` 时只评估并保存缺口任务；`--orders`、`--actions` 可传入其他必要证券，逗号分隔。`--history-sessions` 默认为 121；`--no-events`、`--no-indices` 可缩小采集范围，但不会把跳过的数据伪装成已准备完成。

默认单次最多500个来源请求、1800秒采集预算，单次来源进程最长45秒，实际timeout受剩余总预算约束。HTTP传输的open必须使用调用方传入的剩余timeout，不暗中截为10秒；外层可终止并回收的子进程保证完整请求的硬截止，响应头等待或响应体慢速滴流都不能越过总deadline。分页来源共用同一来源deadline，每页传剩余时间，不每页重置45秒。可通过 `--max-requests` 和 `--budget-seconds` 限定本次工作。预算用完保存状态，后续可再次显式运行继续。独立命令本身不创建后台任务；已安装并启用的workflow负责其日程范围内的自动续办，沿用同一缓存、缺口和来源冷却。当前安装与启用状态见[实施状态](implementation-status.md)，自动日序见[工作流方案](workflow-design.txt)。

`prepare-data` 返回 `POOL_PREPARED` 或 `UNCHANGED` 时退出码为0；等待、保留旧池或错误为2。`data-sync` 的 `state_scope=requested_fact_collection_not_trading_readiness` 明确表示本次请求的采集覆盖；请求指数缺失时整体为 `PARTIAL`，已就绪股票仍可独立查询。每股记录 `prices`、`events`、`event_coverage` 及问题，顶层 `event_conflicts` 明示冲突；覆盖齐全但来源冲突仍可返回 `COMPLETE`，不能据此结算或交易。候选池是筛选结果，不是下单名单。

## 统一查询、冻结与修订

```sh
# 估值使用单日行情；规则策略输入使用 121 日窗口。
./.venv/bin/python -m china_stock_sim.market data-query \
  --code SH600000 --as-of 2026-10-27 --purpose valuation
./.venv/bin/python -m china_stock_sim.market data-query \
  --code SH600000 --as-of 2026-10-27 --purpose rules

# 来源日历不够时明确返回等待，不猜下一交易日。
./.venv/bin/python -m china_stock_sim.market data-calendar \
  next_session --date 2026-10-27

# 显式保存当前输入快照，不联网。
./.venv/bin/python -m china_stock_sim.market data-freeze \
  --codes SH600000,SZ000001 --as-of 2026-10-27 --purposes valuation,rules

# 查看来源事实修订。
./.venv/bin/python -m china_stock_sim.market data-revisions \
  --code SH600000 --kind bar --start 2026-10-26 --end 2026-10-27
```

冻结命令返回 `snapshot_id`，可用 `data-snapshot <snapshot_id>` 校验并读取。快照允许记录等待或缺失状态，因此“已生成快照文件”不代表里面每种用途都 `READY`。

`data-query` 的用途包括 `valuation`、`llm`、`rules`、`execution`、`events`、`event_settlement`、`index`。其中 `llm` 只检查21日策略输入，不调用模型；`rules` 检查121日输入；`execution` 还需要调用方通过 `--rule-context` 提供规则JSON，并检查证券身份、状态和权益。未知交易特殊状态、缺失权益或缺规则不能默认为可成交。

查询返回 `state`、`data`、`issues`、`observation_ids`。状态为 `READY`、`PARTIAL`、`MISSING`、`WAITING_DATA`、`CONFLICT`、`CORRUPT`；只有 `READY` 表示这次具体查询的条件满足。价格与金额采用十进制字符串，调用方应使用 `Decimal` 计算。

`--known-at` 接受带时区的时间，例如 `2026-10-28T22:30:00+08:00`。默认使用实际当前时点；指定过去时点时排除后来才取得的事实。现在下载的历史行情不会变成过去盘前已知的数据，未来价格不返回就绪。信号序列只使用截止时点已知的分红/转增事实，不直接套用一份今天的全历史复权价格。

## Python 接口

```python
from pathlib import Path

from china_stock_sim.market.contracts import DataRequest
from china_stock_sim.market.service import MarketDataService

root = Path.cwd()  # 在 simulate-china-stocks 仓库根目录运行
service = MarketDataService(root)

# 只读：不建目录、不联网、不修改账户。
result = service.readiness("SH600000", "2026-10-27", "rules")
print(result.as_dict())

# 明确的更新入口；调用方传入需要维护的证券。
request = DataRequest(
    as_of="2026-10-27",
    candidates=("SH600000",),
    holdings=("SZ000001",),
)
progress = service.update(request, download=True, max_requests=20)
```

还可调用 `prices`、`calendar_operation`、`security`、`events`、`event_coverage`、`event_settlement`、`signal_prices`、`execution_facts`、`pool`、`freeze`、`read_snapshot`、`revisions`、`evidence_manifest`。调用方通过公开契约获取事实，不自行解析准备版本的文件名，不直接修改市场索引。

## 账本使用的新增数据接口

以下能力并入同一数据层及原命令，不改变命令1/月末、命令2/每日18:10的节奏，也不重下已经合格的121日历史行情。

**按动作查询权益。** `event_settlement(..., operation=...)`及`readiness`支持`eligibility`（登记资格事实）、`cash_accrual`、`share_accrual`、`cash_payment`、`share_delivery`，默认`settlement`保留整件检查。每件返回`operation_date`与`operation_applicable`；`READY`只表示事实齐全，账本仍须判断到期、持仓资格及是否已记账。明确零分配返回“不适用”，未知金额不当零。

```sh
./.venv/bin/python -m china_stock_sim.market data-query \
  --code SH600000 --as-of 2026-10-28 --purpose event_settlement \
  --event-operation cash_accrual --event-ids 已保存的事件ID
```

`--event-ids`须替换为实际查询所得ID；省略时检查当日事项及未定日风险。`data-freeze`也支持`--event-operation`和`--event-ids`，用途须含`event_settlement`，显式事件ID冻结限定一个证券；快照保存动作与选择条件。税基缺失单列，不挡住事实齐全的现金分配动作；分配金额/日期本身冲突仍拒绝相应事件。

**历史未完结权益范围。** 调用方传通用日期，不传来源专用年份：

```python
from china_stock_sim.market import DataRequest, PendingEventRange

request = DataRequest(
    as_of="2026-10-28",
    pending_event_ranges=(
        PendingEventRange("SH600000", "2023-01-01", "2023-12-31"),
    ),
    include_indices=False,
)
# service已按上文初始化。显式download才可能联网。
result = service.update(request, download=True, max_requests=20)
```

仅出现在`pending_event_ranges`里的证券只刷新权益，价格显示`NOT_REQUESTED`，不会顺带下载它121日行情；与持仓/候选等重叠时照常维护原行情窗口。范围须`start <= end <= as_of`，不能与`include_events=False`组合；未来已公告付款/上市事项从事件视图读取。`event_ids`是可选追踪引用，范围采集成功不代表这些ID一定存在，账本仍须显式查询；源ID变化可能需要账本case_id归并。

命令2可用`--pending-event-ranges 文件.json`接收同样范围。文件为列表，例如`[{"code":"SH600000","start":"2023-01-01","end":"2023-12-31","event_ids":[]}]`。显式提供该文件且不提供`--codes`时，不自动引入默认候选池；纯权益补查可配合`--no-indices`。普通命令2不带新选项时仍默认维护生效池。正式流程由调度合并候选、持仓和待办范围，无需用户每日额外操作。

每个范围分别保存覆盖证据与`refresh_complete`。已有旧coverage的READY不能覆盖一次失败、暂停或中断的刷新；请求预算耗尽、来源暂不可用亦保存待办，离线检查不把它们清为完成，恢复后只补未成功请求。旧事实仍供历史追溯。

**税基事实。** 事件新增`share_face_value`、`capital_source`、`tax_basis_note`及逐字段`tax_facts`。字段状态为`READY/UNKNOWN/CONFLICT`；未知值为null，原因明确。`capital_source`已知值为`STOCK_PREMIUM/OTHER`，不能据未知猜免税。`tax_readiness`仅汇总全部额外字段，账本应按当前动作实际所需字段检查，不把它当现金分红总开关。

目前接入的BaoStock、东方财富分配适配器不提供上述三个额外字段，能力明确声明`UNSUPPORTED`。旧缓存读取时补充UNKNOWN，不修改原件；现金金额、分配比例和日期继续原样使用。已知税基值必须有可核验原件、字段、事件与适用日；引用和内容不符时拒绝，不接受只有一个自称“有来源”的数值。完整状态及验收证据见[数据层清单](data-layer-backlog.md)。

适配器以后提供已知值时，`sources`同时保存`provider/raw_reference/row_index/source_field/event_id/applicable_date/value`。程序核对原件`rows`中从0起的指定行及直接字段、证券/事件身份、适用日和实际值，并验证原件依赖与取得时点；任何不符返回`CORRUPT`。查询和快照回读采用同一校验，不能用任意原件引用为数值背书。

**备份用的公开证据清单（D08）。** `service.evidence_manifest(snapshot_ids, observation_ids=...)`只读校验既有快照、观测与原件的递归依赖，返回文件路径、SHA-256、快照/观测ID及需要归档的市场索引范围。账本据此保存备份和恢复所需证据，不解析数据层私有文件或直接拼接底层索引。缺失、损坏或ID不合法时明确失败，不会采集、修复、创建新市场事实或修改 `market.sqlite3`。

```python
# 参数来自已经保存的快照和公开查询结果，不自行编造ID。
manifest = service.evidence_manifest(saved_snapshot_ids, observation_ids=saved_observation_ids)
```

该接口已通过[账本建设验收](../logs/ledger-build-2026-10-29-verification.json)，对应回归纳入10月30日1519项统一验收。生产账本已完成显式备份迁移，原三账户及注资身份、金额保留；账本迁移本身不修改市场索引，日常采集仍按正常流程追加市场事实。账本迁移和恢复不要求重新采集已合格历史行情。

账本另外提供 `resolve_tax_holding_rule`、`resolve_event_identity` 两个有依据的维护入口；它们属于账本，数据层无需新命令。`configs/tax_holding_rules.toml`默认空规则，未知税务起点保留 `UNKNOWN/RULE_PENDING`，不猜测日期；原来源未知与冲突仍按用途隔离。备份保留最近30份完整成功日备份，不完整备份不会淘汰最后一份完整备份。

## 来源能力

| 来源 | 已接入范围 | 必须保留的边界 |
|---|---|---|
| BaoStock 0.9.4 | 日历、证券快照、全市场批量日线、逐股不复权历史、复权因子、分红线索 | SDK 成功、空返回、字段有效与用途就绪分别检查；分页结束错误也拒绝整次结果 |
| 腾讯免费 HTTP | 主板股票备用不复权价格、上证综指/深证成指/沪深 300 价格 | 前收、ST、停牌未知；股票量额按源单位转换并声明精度；指数量额单位未认证时保持未知 |
| 东方财富免费 HTTP | 股息与股份分配信息，补充并核对权益日期和单位 | 部分支付/股份到账日期缺失，复杂事件缺席不能据此认证；未知字段不补造 |

主源失败时按能力使用备用价格源；备用价格可支持相应价格用途，不会凭空补齐主源特有的交易状态。后续主源恢复可补齐能力。来源有冲突时保留原件与冲突信息，不静默混合成一条看似完整的数据。

## 中断、重试与修复

请求由来源、适配版本、查询和参数标识，开始前保存状态，成功校验后才记完成。`attempts`累计实际源请求，仅作审计；网络/暂未发布等可重试失败另用`retry_failures`记录连续失败，依次选择60、300、900秒退避，超出档位保持末档，不能因历史成功请求多就加长本次首败等待。成功校验清零连续失败；预算暂停、来源冷却/不可用、主动中断与遗留RUNNING转PAUSED不增加该计数。旧记录没有新字段时，已知OK从0开始；无法区分成败的旧失败/中断记录保守保持原失败档位，后续可信成功再清零。已有`next_retry_at`及`server_retry_at`不追溯缩短或改写；服务端`Retry-After`继续记录并遵守，显式刷新不能越过服务端等待。`--refresh`/`--repair`维持原显式语义，不借本次修复改变重取或修复授权。单次运行不无限重试，到期后仍用同一命令补缺。

本轮真实问题（2026-11-01，命令2目标2026-10-30）：首轮权益刷新仍有等待，定位到`market/update.py`把累计`attempts`同时用于失败退避，历史成功会使后来首次可重试失败直接进入较长档位。最小修复采用上述两类计数，不改旧请求证据、保存的重试时刻、服务端等待或已有合格历史。专项、1681项全量回归与Ruff均已通过；按保存deadline续跑后，本轮只读终验COMPLETE；首轮PARTIAL及失败原因保留原回执，不以修复覆盖历史。该段只记录13:53旧池命令2阶段；随后命令1的真实运行与新池用途另见文首。

旧池命令2 HTTP超时修复（2026-11-01 13:53阶段，代码已冻结）：续跑发现`market/providers/http.py`原`open(timeout=min(timeout,10))`与上层传入的剩余预算不一致，已最小改为`open(timeout=timeout)`。只去掉这处隐藏10秒截断，外层硬总时限、分页预算、Retry-After、来源地址、BaoStock以及45秒配置/命令参数保持不变；不修改已保存的重试截止来提前重问。新增5项离线回归覆盖实际传参和真实本地假响应滴流进程被终止/回收，HTTP/权益专项92项、市场专项617项与Ruff通过，详见[HTTP修复核验](../logs/data-sync-2026-10-30-http-timeout-validation.json)。该次最终全量1681项/63.03秒及Ruff已通过；原deadline到期后的真实续采和只读终验均COMPLETE，该完成结论由实际数据验收支持，不由专项测试代替。

`data-sync --download --refresh` 显式重新检查来源订正；新事实追加保存，已冻结输入不被覆盖。当前权益批次为空也会保留“撤销/无当前成员”的事实，历史时点仍能读取旧版本。

缓存损坏被标记为不可用。`--repair --download` 是显式修复入口：隔离损坏原件后补取，保留证据；不会通过删除整个数据库或重建账户来恢复。旧快照依赖的原件缺失或损坏时，旧快照继续报不可用，不能用新数据冒充其原始输入。冻结池损坏或冲突不得静默换池。

普通查询与快照读取核验来源内容、索引元数据和合成事实依赖。`data-status` 只读保存的进度，不重新校验所有产物，不能据其旧 `price_ready` 值进行交易。

这些采集、校验、退避、续跑和缓存恢复机制均由普通程序执行，不依赖LLM或聊天助手监控。试运行可检查并修正代码；正式运行超出预设恢复范围时须保存证据、暂停受影响项并展示原因，不能转交LLM猜补数据。当前再次运行命令可续跑，自动触发及重启恢复由现行workflow组织，生产须显式启用与安装；关机时程序不能执行。

## 事件分类离线重建

SC01—SC04于2026-11-01完成实现、生产重建及独立验收；具体当时结果见[封口终验](../logs/event-classification-repair-2026-11-01-final-acceptance.json)。

此维护入口只修正已有原件的`kind/status`解释，不改变命令1/命令2，也不改变`--refresh`重新检查来源订正或`--repair --download`补取损坏缓存的含义。本轮规则仅排除会计短语“合并报表”对复杂事项的误触发，保留真实复杂动作；东方财富精确进度“股东大会决议通过/预披露”映射`PROPOSED`、“股东大会否决”映射`CANCELLED`，取消/终止优先。原实施和未知语义保持，不补造分配条款或实施日期。

以下为确定的CLI契约；日期、代码和`PLAN_SHA256`均须替换为本次实际范围及计划返回的摘要。该入口已经生产验收；其后最新程序回归与运行状态见统一入口。

```sh
# 默认仅计算计划；无写入、无网络。
./.venv/bin/python -m china_stock_sim.market data-rebuild-events \
  --as-of YYYY-MM-DD --codes SH601166,SZ000063

# 同一明确范围，核对计划摘要后显式应用。
./.venv/bin/python -m china_stock_sim.market data-rebuild-events \
  --as-of YYYY-MM-DD --codes SH601166,SZ000063 \
  --apply --expected-plan PLAN_SHA256
```

- **范围和拒绝条件：** `--codes`必须显式提供；只扫描这些代码各来源当前最新`event_batch`，任一当前批次`date`晚于`--as-of`即拒绝，不选择旧批次替代。支持现有BaoStock和东方财富结构。缺批次、未知结构、原件/依赖损坏、成员清单不一致、重算出现`kind/status`之外业务字段变化，任一问题均拒绝整单；不扩大到未请求代码，不隐式采集。
- **计划与应用：** 默认返回范围、现行批次/成员、分类差异与计划摘要，不写文件/数据库或联网。`--apply`必须同时给`--expected-plan`；应用取得共用market writer锁后重新计算；当前已无变化时返回`UNCHANGED`且零新增，可重复此前已应用的plan_id。仍有变化时必须比对expected-plan，摘要失配拒绝整单，不能用过期计划覆盖新的输入。摘要只绑定可审阅的当前输入与变化，不把预览时间当原件取得时间。
- **完整批次原子发布：** 从原件运行唯一当前normalizer，仅追加有`kind/status`变化的事件；新批次重用未变成员的旧观测ID，引用变化成员的新ID，保留完整成员集合。所有受影响事件和批次同一SQLite事务提交；中断或异常不能发布半批、不能部分代码成功。来源原文、金额、日期、税事实及真实冲突保持。
- **时间与证据：** 原`raw_ref/provider/fetched_at`保留，新观测`available_at`取实际修复时间，追加normalization metadata中的修订及previous observation ID。旧原件/观测/batch/冻结快照不改写；旧知识截止仍可读取旧事实，新解释不回灌原采集时点。正常命令2同日成功缓存复用、命令1准备权益再导入必须复用同原件、相同当前解释的已存修正版及其available_at；已有原件确有新解释时使用实际规范化available_at，batch可用时间不早于成员；真正新原件仍按来源fetched_at可用。不得通过缓存复用/重新导入将修正以旧fetched_at回填过去，原来源fetched_at及coverage新鲜度不变。
- **范围外状态与幂等：** coverage不重建、不更新时间，原source completeness及依赖保持；不改变requests/jobs、价格、两池、准备任务、账本，不把离线重建记成来源刷新。重复执行无差异返回`UNCHANGED`且无写入。全程无网络、无模型、无全量历史下载。

应用后须按同一目标日、知识截止和相关用途重新核验，分别保留真实来源冲突与仍缺实施日期的等待；分类修正不自动授权记账或交易。完整SC01—SC04验收见[数据层清单](data-layer-backlog.md)，完成状态另以真实实现、隔离故障测试及生产重建结果为准。

## 输出与页面

| 仓库内路径 | 内容 |
|---|---|
| `data/state/market.sqlite3` | 共享市场事实索引、请求与任务状态 |
| `data/raw/market/` | 按内容摘要保存的来源响应、合成事实及依赖 |
| `data/normalized/snapshots/` | 带知识时点、查询结果和来源 ID 的不可变快照 |
| `data/raw/quarantine/` | 显式修复时隔离的损坏内容及原因 |
| `data/raw/baostock/` | 首次/月末准备缓存，带请求身份与校验 |
| `data/state/preparation/<准备任务ID>/` | manifest、冻结池、失败请求与拒绝原因 |
| `data/normalized/preparation/<准备任务ID>/` | 准备流程的行情和 `CLUES_ONLY` 线索产物 |
| `data/acceptance/` | 小样本真实来源验收原件与结果 |
| `reports/preparation/index.html` | 展示层生成的首次/月末准备进度页，不代替日常增量状态 |
| `reports/data-sync/index.html` | 最新增量采集核验的静态页面，展示行情、字段、权益覆盖和各用途状态 |

独立入口只输出 JSON 并保存采集进度。项目总入口 `china-stock-sim prepare-data` 在数据任务返回后调用展示层生成进度页；报告失败单独提示，不撤销采集结果。页面每 30 秒重新加载已生成文件，不表示后台任务正在运行。进度页链接到账本页，但不修改账本，日常增量状态可从 `data-status` 查看。

## 旧逻辑的复用来源

以下仅为迁移来源记录，不构成运行路径或依赖：

| 旧工程逻辑 | 本仓库落点 | 采用方式 |
|---|---|---|
| `src/ashare/data.py` 的 `_worker_unlocked` | `market/providers/baostock.py` | 子进程硬超时、固定结果标记；移除旧入口与审批依赖 |
| `scripts/probe_worker.py` 的 `bsframe` | `market/providers/worker.py` | 保留分页前后错误检查，改为白名单查询 |
| `src/ashare/bao_inputs.py` | `market/quality.py` | 明确停牌规则与严格数值校验 |
| `src/ashare/rolling_fetch.py` | `market/preparation.py`、`storage.py` | 请求身份、日期集合及恢复思想 |
| `src/ashare/universe.py` | `market/universe.py` | 完整窗口、确定性排序；采用当前 121/20/100 参数 |

未迁入旧账本、旧云端审批、历史回测流程、源码哈希闸门或人工权益复核链。新的日常服务、HTTP 来源与统一契约均在本仓库维护。

## 消费时的当前事实与历史记录

当前身份、股票池、覆盖证明和指定日历窗口，只校验本次实际使用的观测；历史损坏原件不会阻断完整的新事实。旧快照若依赖该坏原件仍明确损坏，不静默换成新数据。当前生效池自身损坏时，准备入口返回待核，不按“没有池”重新选股。

权益原始视图用 `--purpose events`，保留全部当前已知冲突；日结所需的事实齐备检查用 `--purpose event_settlement`。用途查询先按目标日或信号窗口选择相关事件，再判定冲突，不让无关旧冲突阻断所有日期。账本对已登记但未到账/未上市的事项通过 `--event-ids` 指明事件ID继续追踪；这些历史待办、当日事件及未定日事项仍严格校验。数据层不根据当前账户持仓自动推断这份清单。

来源小数位不同仅在完整分配方案一致、数值能按各自精度正确舍入且绝对差不超过0.000001时归并，保存 `precision_notes` 和原件证据。没有上述依据或存在真实金额矛盾时，继续标记冲突，不以放宽容差代替核验。

月末准备worker修复（2026-11-01）：首轮14:46:35—14:55:54共322次请求，321成功、1失败；外层POOL_PREPARED/退出码0只表示池已生成，内部仍PARTIAL且clues_downloaded=false。SH603256 adjustments在BaoStock退出阶段返回10001010，原来错误归为CONFIGURATION。最小修改仅将退出阶段该码归为可重试NETWORK，仍拒绝响应、不接受失败字段或行；查询/分页原错误优先，其他阶段和未知码不变，既有失败回执及重试时刻保留。新增8项回归，专项94项与Ruff通过，随后全项目1689项/64.38秒及Ruff通过。同命令续跑于14:59:39.244—14:59:47.746仅重试该1项并成功；累计323次实际请求、322逻辑全OK，内部PREPARATION_COMPLETE。修复不改变来源、选池/历史参数、45秒预算、配置或命令用法；来源线索下载齐全仍须单独核验规范权益覆盖和各用途。见[修复专项](../logs/month-end-preparation-2026-10-30-worker-fix-verification.json)。

## 日期化运行记录与数据库

- `logs/event-classification-repair-2026-11-01-{backup,plan,apply,repeat,run}.json`：2026-11-01分类修正与离线生产重建证据，12事件/12批次追加、重复UNCHANGED、现场用途已复核；独立保护终验PASS。
- 同前缀`pytest.txt/xml`、`ruff.txt`：2026-11-01分类修复时1771项/64.61秒、错误/失败/跳过均0及Ruff通过；旧1689记录保留worker阶段范围。

- `logs/month-end-new-pool-2026-10-30-run.json`及`-command.json`：当前第三步运行/回执，显式新池100代码、12次权益请求、退出码0。此前命令1首轮和续跑由`month-end-preparation-2026-10-30-run.json`、`-resume-1-run.json`保留。
- `logs/month-end-new-pool-2026-10-30-verification.json`及`-final-acceptance.json`：2026-11-01 15:08新池采集/事实覆盖COMPLETE、实际12次来源请求及修复前用途统计和两池/原观测/账本/准备状态保留。命令1的774项检查及6股覆盖缺口仅保留在`month-end-preparation-2026-10-30-verification.json`历史范围。
- `logs/month-end-preparation-2026-10-30-worker-fix-verification.json`及同前缀pytest/ruff/tested-source-manifest：退出错误分类修复和1689项回归。此前`data-sync-2026-10-30-final-acceptance.json`保留13:53旧池采集与1681项测试范围，不替代新池证据。
- `data/state/market.sqlite3`：当前共享行情、证券、日历、权益、覆盖、候选池，以及程序自身的 `requests`、`jobs` 状态。
- `data/state/preparation/latest.json` 及对应 `manifest.json`：最近一次首次/月末准备的进度和证据；它们的目标日仍可早于增量截止日，不表示日常增量尚未完成。
- `logs/preparation-runs.jsonl`、`logs/data-sync-runs.jsonl`：追加保存历次命令回执；失败、中断和恢复用于审计，不能取代上面的最新结果。

`clues_downloaded=true`只表示命令1来源线索已保存，不保证无冲突或规范权益覆盖完整。本次14:59准备时6只新增成员覆盖仍等待，15:08第三步显式新池命令2现已补齐，当前覆盖100 READY、事件98 READY/2 CONFLICT。外层POOL_PREPARED/退出码0仍不能单独证明内部准备完整，应检查PREPARATION_COMPLETE、price_ready/clues_downloaded和请求失败数；之后再查具体用途。数据完成不自动生成账本或启动交易。

历史准备、小样本验收、先前失败和程序修正的详细过程统一保留于[调整记录](architecture-change-log.md)及其引用的日志，不在本文件重复维护另一套数据状态。

## 当前月末结果与按池范围核验入口

月末三步的已执行事实见上述日期记录；分类修复后及后续当前用途统一见[实施状态](implementation-status.md#最近已核运行与验收边界)与[机器状态](../logs/project-current-state.json)，不把任一旧阶段统计当成当前查询结果。

下列既有脚本核验10月30日生效的旧池（不联网、不改数据库）；不会自动改为11月2日新池，不能覆盖月末独立核验结果：

```sh
./.venv/bin/python scripts/verify_data_sync.py --as-of 2026-10-30 \
  --baseline logs/data-sync-2026-10-30-baseline.json \
  --output logs/data-sync-2026-10-30-verification.json
```

返回码0仅表示脚本所核验的旧池采集范围完成，2表示该范围仍有缺口。程序分别保存事实覆盖、最新范围任务、后续相关失败、字段完整性和各用途状态，不能把已有覆盖当作后来失败的刷新成功。数据采集页为验收时点静态报告；原preparation页继续对应首次/月末选池记录，账本页不伪造今日交易。

## 策略及多账户依赖复核（2026-10-30）

[strategy-design.txt](strategy-design.txt)已完成设计与依赖审查。现有数据接口满足规则策略121日、LLM21日调整价格、原始量额/价格、N日生效池、日历、资格/权益及证据查询；默认三策略无需新数据功能；自定义窗口的signal_prices实现和SD01公开Protocol声明/消费者验收均已完成。指标/百分位/排名和LLM20候选排序由策略程序计算，不是需要另下载的字段。

多账户共享行情，由流程汇总候选/持仓/未决订单/未结权益范围，按既有DataRequest传入；自定义长窗口每轮持续传实际最大history_sessions（最多500），超出单请求证券数量上限时分批。未知数据仍按用途隔离，不把未披露字段补0。命令1首次及月末最后交易日18:10后、命令2每交易日18:10后及月末显式补新池缺口均不变；日常增量复用合格历史，月末新批次按其来源版本流程准备历史，旧观测完整保留。本次账户扩展已完成账本先备份再显式2→3，原19表逐行逻辑摘要保留，该次SL迁移期间市场库SHA不变。旧池命令2已补至10月30日，月末命令1已追加11月2日生效的新池；原池与旧准备任务保留，第三步现已显式100新池代码补齐原6股缺口。

只读接口核查见[数据依赖审计](../logs/strategy-design-2026-10-30-data-audit.json)，三策略已接入上述公共查询并完成隔离消费者/闭环专项，本轮全量及SA完整验收进度见[当前状态](implementation-status.md)。

SD01数据支持已完成：公开MarketDataReader.signal_prices及现有服务一致，15项新增消费者测试、当时全项目1278项及Ruff通过。详见[SD01验收](../logs/data-sd01-2026-10-30-verification.json)。命令1/2及历史缓存不变。SM01统一脱敏与公开审计接口已实现并验收，见[SM01验收](../logs/model-sm01-2026-10-30-verification.json)；SX01已实现并验收，见[SX01验收](../logs/execution-sx01-2026-10-30-verification.json)；SL01—SL04已实现并验收，三策略与完整账户业务已接通独立应用，整日流程已实现，WF验收已通过。10月30日SX01执行消费者建设只补策略原件核验、账本signal_prices查询允许范围和递归归档，不修改数据采集程序，不涉及数据库迁移或历史重下。

SI01既有动态账户/冻结身份/中文审计已验收，见[组件方案](interface-design.txt)及[浏览器证据](../logs/interface-si01-2026-10-30-browser-acceptance.json)。本次SL新增政策/决策API与CLI另行验收，不扩大原浏览器通过范围。

本轮策略消费者契约补充（2026-11-01）：本轮策略消费者联调修正signal_prices公开issues契约：历史字符串问题以{"kind":原问题代码}返回，原对象问题及观测ID保留，符合DataResult对象列表约定；不消除来源冲突，不改变行情/权益事实，无需原件重建或重新下载。完整验收见[当前状态](implementation-status.md)。

工作流依赖补充：首次失败回执`job.issues`公开已计算的`next_retry_at`及存在时的`server_retry_at`，流程据此遵守原冷却；不修改退避计数、Retry-After或事实。`WorkflowData.inspect`只读；`collect`与月末准备是显式写任务。月末必须检查内部`PREPARATION_COMPLETE`、`price_ready`和`clues_downloaded`，不能只凭外层`POOL_PREPARED/exit0`认定完成；然后显式维护新池代码，as-of仍是已完成D。

自动编排的执行时段：18:10仍是当日数据可采集门槛；业务自动触发在17:00—19:00跳过，19:00后的首次检查或开机补办已到期数据和后续日结/规划。独立命令1/2及月末先2、再1、新池再2的顺序不变。已验证相同来源coverage可精确复用，真实新来源和必要缺口保留采集证据；不为解除未实施预案误阻断而重下合格历史。

历史实施、测试、安装及云端部署的发生时事实统一见 docs/architecture-change-log.md 的日期化证据索引；本附件只定义当前契约，不重复维护历史运行快照。
