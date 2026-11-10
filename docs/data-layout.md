# 程序、配置与数据目录

本机菜单栏就绪指示器的现行契约见[ER01—ER12](evening-readiness-design.txt)。显示程序仅消费既有账本、日历及验证证明，不新增市场数据或金融表。`deploy/macos/`保留唯一原生源码和当前应用；`data/state/shutdown-indicator/instance.lock`用于显示实例互斥；`data/state/writer-owner.json`由原业务外层写锁持有者临时发布，供只读观察核对锁归属，释放时清除，不是金融数据；`logs/shutdown-indicator/latest.json`仅保存最新诊断，变化日志`events.jsonl`及单个轮换`events.previous.jsonl`按大小上限保留。它们不是金融事实或第二套运行版本，不上传云端；业务审计仍使用原有持久记录。


现行上传按docs/publishing-schedule-design.txt的PS01—PS12使用上海09:00/21:00及最近已到时点去重；三任务分工和15分钟补检保持。2026-11-02的PS测试/安装/RunAtLoad证据保留在原日期日志，当前启用与维护状态见唯一状态入口。
当前进度、真实数据截止、用途就绪与最新回归只在[实施状态](implementation-status.md)维护；机器读取[当前项目状态](../logs/project-current-state.json)。本文保留本层契约及明确日期的验收事实，日期日志不随当前状态刷新。

本仓库将可复现核心程序、通用配置及核心安全模板放进Git；独立云端发布程序、其全部配置/模板、专用部署与测试、真实凭据和运行数据仅保留本机。公开克隆不依赖发布层即可使用核心功能；本机完整环境仍保留原三个任务。唯一现行根目录为当前项目目录（`/path/to/simulate-china-stocks`仅为示例），所有相对路径以此为根；迁移机器后使用目标目录作为 `--root`。不自动查找父目录旧项目的环境、数据或账本。

最新策略/可新增账户方案见[strategy-design.txt](strategy-design.txt)。SD01/SM01/SX01/SI01既有验收有效；SL01—SL04开户/政策/决策/模型绑定在2026-11-01完成schema2→3及恢复验收；现行程序存储契约已推进schema4，真实切换状态见唯一入口。SL迁移时市场库不变；其后日常增量/月末准备正常追加市场事实，命令1/2及采集节奏不变；三策略独立规划/发布/恢复已实现，整体调度与流程已实现，WF01—WF32全部验收通过；本轮验收进度见唯一入口。

## 程序分层

| 目录 | 职责 |
|---|---|
| `src/china_stock_sim/common/` | 不依赖业务存储的费用与规则纯计算；各业务层共用，不另立业务层 |
| `src/china_stock_sim/runtime/` | RO01—RO12运行记录、资源/身份、只读查询与运行归档；共享支撑，不依赖策略/账本或新建业务层；实际实现与验收见当前入口 |
| `src/china_stock_sim/market/` | 可独立运行的数据服务、来源适配、保存、查询与质量检查 |
| `src/china_stock_sim/strategies/` | S1/S1-LT纯特征/排名/数量与LLM固定输入/严格解析/硬截止；独立策略CLI |
| `src/china_stock_sim/models/` | 模型接口、配置与服务适配；`audit.py`提供统一脱敏和configuration/request/result公开审计快照，不决定交易、不改账本 |
| `src/china_stock_sim/execution/` | 纯验证器、撮合器、公开事实组装及只读执行命令，已验收 |
| `src/china_stock_sim/application/` | 策略准备/规划/发布/恢复，执行读取/封存/结算与规则维护阻断；workflow、workflow_data、workflow_ledger组织整日顺序与恢复 |
| `src/china_stock_sim/ledger/` | 完整L01—L24代码已实现：事务、账户/计划/预留、成交/费用、FIFO批次、权益税、日结查询、恢复/更正/备份及独立命令；统一验收已通过 |
| `src/china_stock_sim/reporting/` | 从已有账本或数据状态生成独立本机页面，属于公开核心 |
| `src/china_stock_sim/publishing/`（仅本机） | 唯一独立发布层：包、传输、状态、时点、独立配置与上传任务；不随Git分发，核心可选接入 |
| `src/china_stock_sim/scheduler/` | 工作区/公共写锁辅助；纯日程规划、耐久journal、共用写锁及显式launchd维护 |
| `scripts/`、`tests/` | 环境和本机入口、核心行为验证；云端专用测试仅本机，混合测试保留核心覆盖 |

业务调度性能方案保留在唯一附件 `docs/strategy-performance-design.txt`。`scripts/benchmark_strategy_performance.py`、`scripts/benchmark_market_update_performance.py`、`scripts/benchmark_statement_review_performance.py`各自只在新建临时夹具中比较固定输入，禁止对正式根运行业务或调用来源/模型。日期化回执分别保留原SP与本次 `logs/workflow-performance-2026-11-10-*`；对照源码只作临时测试基线，不安装为第二套程序。`market/raw_validation.py`是market层内部有界单次验证支持，不建立业务库或新持久缓存；账单投影仅驻留本轮内存，既有验证证明目录继续沿用。完整文件盘点使用有摘要的压缩JSONL与小型用途/处置清单，避免把全树正文复制到当前入口。

具体完成范围及[六层依赖图](implementation-status.md#分层依赖关系)见实施状态，唯一现行设计见[架构方案](architecture.txt)。application包含策略/执行子流程及workflow整日编排，不额外增加业务层。

账户管理的当前边界：配置只为空库提供初始种子；config/schema/CLI和看板已解除固定三账户/本金限制，并从持久登记查询。ledger/store.initialize对已有库只核验，不根据种子新增、删除或改本金。list_accounts/get_account_profile已提供；运行中原子开户/base政策接口已通过SL01验收，不能仅在TOML加一行冒充开户。真实库仍为原三账户；当前观察起点、政策和计划状态见唯一当前入口。

[账本附件](ledger-design.txt)是总架构的功能、接口、表结构与验收细化，不是第二个架构版本。数据D01—D20均已实现并验收，包含D08公开证据归档及账本/执行消费者所需接口。账本不解析数据私有缓存、不直接采集，来源未知仍通过契约表达。

数据层已独立：`python -m china_stock_sim.market` 是独立入口，`MarketDataService` 是其他层读取数据的公共接口，`DataRequest` 是调用方传入取数范围的契约。数据层不反向导入策略、模型、账本或报告实现，也不直接读取账户决定取数范围。

| 数据模块 | 职责 |
|---|---|
| `contracts.py`、`service.py` | 标准请求/结果/快照、按用途的只读查询及明确更新入口 |
| `settings.py`、`cli.py`、`__main__.py` | 独立配置、依赖检查及命令入口 |
| `providers/` | BaoStock、腾讯、东方财富适配与超时/限流/错误分类 |
| `quality.py`、`calendar.py` | 字段、单位、关联一致性、窗口与真实交易日历 |
| `events.py`、`signals.py` | 权益标准化与冲突、因果信号输入、执行事实检查 |
| `freshness.py` | 权益原件的收盘后取得时间校验；递归检查覆盖依赖，防止旧缓存被合成时间误标为新覆盖 |
| `event_operations.py`、`tax_facts.py` | 分别检查权益动作、日期与适用性；税基逐字段来源/未知/冲突，当前来源能力明确声明 |
| `repository.py`、`storage.py`、`evidence.py` | 追加事实、原件、索引、内容校验、原子写入与快照；公开 `evidence_manifest` 导出已核验的递归证据及索引需求 |
| `update.py` | 明确采集、增量、修订、备用源与修复；来源连续故障冷却、请求/任务中断状态及续跑恢复 |
| `pools.py`、`preparation.py`、`universe.py` | 首次/月末准备、候选规则与共享索引导入 |

首次/月末与日常增量共用数据契约和共享索引。准备任务的工作文件保留用于恢复，不能作为其他业务层私自解析的永久接口。报告由应用外层生成，失败不使采集结果失效。

DataRequest新增PendingEventRange历史权益范围，复用同一更新器、日志与SQLite；范围-only证券无需下载历史行情。请求预算/来源暂停保存PAUSED，恢复后旧阻塞可标SUPERSEDED并留痕。旧事件原件不改，补充税基UNKNOWN在读取时形成；当前已知税基值须绑定原件精确行/字段及证券、事件和日期，查询与快照回读均核验。

## 执行层现行目录（核心已验收，SX01已实现并验收）

当前[执行设计](execution-design.txt)对应已实现的`execution/service.py`纯门面、`validation.py`盘前验证、`matching.py`开盘撮合、`evidence.py`公开事实组装、`cli.py`只读命令；`application/execution.py`负责显式封存/提交恢复，`execution_controls.py`读取维护阻断。`execution/base.py`使用`domain.py`的公共执行DTO；`strategies/base.py`保存内部PlanningContext/StrategyDecision，应用层显式映射为同一domain.StrategyProposal，不把模型专用字段直接塞入执行意图。规则选择位于`common/policies.py`，费用位于`common/fees.py`，执行与账本共用；没有重复的execution/policies.py。SX01新增`common/decisions.py`校验中立决策引用与原件，`common/sizing.py`供执行/账本共享原数量算法；`ledger/decision_evidence.py`负责本地原件、提案/订单绑定及递归业务证据闭包。

数据D18—D20已修改现有`market/signals.py`、`service.py`及相关契约/测试；账本LX01—LX05已扩展现有查询、封存、证据、入账和公共DTO。仍用一个market.sqlite3和一个ledger.sqlite3，不增加第三个业务库。计划/日志/不可变证据沿用现有目录；命令1/2和历史数据不变。账本另有`execution_evidence.py`核验冻结输入及逐原因证明，`execution_support.py`提供视图/守卫/批量恢复。详细当前状态见[实施状态](implementation-status.md)。

## 配置与凭据

| 文件 | 内容 |
|---|---|
| `configs/default.toml` | 账户、北京时间日程、交易规则、费用与策略参数；现行workflow消费显式调度/子层开关、会话及阶段预算；实际启用状态见当前入口 |
| `configs/preparation.toml` | 独立数据层共用配置：121 日历史、20 日流动性窗口、最多 100 只、来源和请求预算/重试 |
| `configs/models.toml` | 可提交的模型引用、适配类型和模型名，不含密钥 |
| `configs/execution_rules.toml` | 现行沪深规则及费用来源/有效区间，封存时完整入计划；execution_blocks登记已确认的维护阻断，默认空 |
| `configs/tax_holding_rules.toml` | 税务起点维护规则及官方依据；默认规则为空、状态UNKNOWN，只有明确配置的适用规则才能恢复RULE_PENDING批次 |
| `.env.example` | 不含真实凭据的环境变量模板 |
| `.env` | 隐藏文件，仅本机模型地址、`CHINA_STOCK_SIM_LLM_API_KEY`及覆盖参数，不提交 Git |
| `configs/publishing*.toml` | 发布独立配置及模板，全部仅本机，不随Git分发；真实配置保持现状 |
| `~/.ssh/config`及其IdentityFile | 用户级SSH登录、密钥和代理；不在项目仓库中，自动上传无需保存SSH密码 |
| `deploy/web/` | 本机Web配置、模板及运维说明，整个目录不随Git分发 |
| `deploy/launchd/bin/A股模拟-云端上传` | 本机发布层专用受控启动器，不随Git分发；业务/首页启动器仍在公开核心中 |

数据设置不依赖模型或账户配置；若 `default.toml` 存在，其中重复声明的数据参数会检查一致性，不默默覆盖。数据层不读取 `.env`。模型层按 `KEY=VALUE` 读取 `.env`，不执行 shell，进程环境变量优先；本机文件权限为 `0600`。详见[模型层说明](model-layer.md)。

## 市场数据与账户数据分开

| 仓库内路径 | 保存内容 |
|---|---|
| `data/state/market.sqlite3` | 独立数据索引：`observations`、`requests`、`jobs`，保存来源事实、请求和任务状态 |
| `data/raw/market/<摘要>.json` | 按内容保存的来源原件及合成事实；合成覆盖与池记录列出原件依赖 |
| `data/normalized/snapshots/<快照ID>.json` | 不可变的用途结果、知识时点和事实 ID，读取时再次验证 |
| `data/raw/quarantine/` | 显式修复隔离的损坏内容与原因 |
| `data/raw/baostock/` | 首次/月末准备缓存，保留来源、参数、时间和摘要 |
| `data/state/preparation/<准备任务ID>/manifest.json` | 准备任务、进度、失败原因和每股状态 |
| `data/state/preparation/<准备任务ID>/pool.json` | 冻结候选、排除原因和校验 |
| `data/state/preparation/<准备任务ID>/attempts/` | 准备过程的失败与被拒绝响应 |
| `data/state/preparation/latest.json` | 最近保存的准备进度，不是消费时可用性认证 |
| `data/normalized/preparation/<准备任务ID>/` | 准备阶段标准化行情与 `CLUES_ONLY` 权益线索 |
| `data/state/ledger.sqlite3` | 同一21表账本，现行程序schema4；经济小索引和CAS原件分离。此前2→3迁移及当时零业务计数属于SL历史事实，正式版本、政策、成交和恢复进度统一见当前入口 |
| `data/plans/` | 账本冻结计划、脱敏模型响应及策略相关不可变原件；SX01的core固定为`data/plans/strategy/<content_hash>.json`，其输入/模型子文档按引用递归校验归档；策略过程和最终core均保留不可变引用 |
| `data/backups/` | 账本SQLite一致备份、账本/计划/市场引用证据清单及校验摘要；账本日备份滚动保留最近30份完整成功记录，不完整备份不能淘汰最后完整备份；恢复只写新的隔离目录。独立运行归档见下方runtime子目录 |
| `data/acceptance/` | 真实来源小样本验收原件与结果，不是交易或账户记录 |
| `reports/forward_current/` | 基于现有账务生成的初始页或业务页和逐日账单CSV；未开始观察时展示初始账户；当前生产页按实际账户、冻结计划及账务记录展示 |
| `reports/preparation/` | 最近首次/月末准备状态生成页面，与账本页分开 |
| `reports/data-sync/` | 最新增量采集核验静态页，区分采集完整性、字段完整性和用途就绪 |
| `logs/` | 程序执行结果、追加运行/进度日志、逐股验收及诊断记录 |
| `logs/runtime/YYYY-MM-DD/<invocation_id>.jsonl` | 三任务逐调用START/EVENT/END、业务关联、性能与覆盖诊断；默认原始明细30天，先验证归档再按完整保护清单清理 |
| `data/normalized/runtime-evidence/objects/<sha256>.evidence` | 内容地址源码原字节，仅审计，不可运行旧版本，不含密钥 |
| `data/normalized/runtime-evidence/identities/*.json` | 源码/依赖声明清单及脱敏配置公开语义摘要，按调用引用 |
| `data/backups/runtime/` | 运行JSONL与引用身份/源码的带SHA256清单压缩归档；内容变化可追加新档，原件和旧档保留，不套用账本最近30份轮换 |

两个 SQLite 文件职责不同，均不能因故障而随意删除。市场索引保存行情、证券、日历、权益、覆盖、池版本等事实，不保存账户余额。现行程序新建`user_version=4`、21表；此前1→2及2026-11-01的2→3保留历史验收，当前正式版本和schema4切换结果见唯一状态入口。此结构迁移不是同时维护两套业务版本；读操作不自动迁移，不删库重建。HTML 可以重建，编辑 HTML 不会改变真实账户。

完整账本逻辑表、DTO及迁移要求统一见账本附件第五、十一、十九节。此前1→2及2→3的生产迁移/恢复事实分别见[10月29日验收](../logs/ledger-build-2026-10-29-production-acceptance.json)和[11月1日SL验收](../logs/ledger-strategy-support-2026-11-01-production-acceptance.json)。现行schema4隔离compact、计划/决策子步骤与正式切换分开留证，当前版本/结果只在唯一入口维护；不得把旧ALREADY_APPLIED或READY回执当作本次已迁移。

## 时间、来源和修订

每条市场事实保留来源、实际取得时间 `fetched_at`、可用时间 `available_at` 和原件引用。行情交易日与实际取得时间不同；现在补取过去数据不能声称过去盘前已经知道。查询默认使用当前实际时点，也可传入带时区的知识截止时点。其他层另行记录计划实际生成、冻结与处理时间。

来源订正以新事实追加保存。读取按所选知识时点选用相应版本；已冻结快照固定事实 ID，不随最新修订漂移。当前权益批次还能表达来源记录撤销，避免旧事件重新出现在当前事实中。

普通消费查询与快照读取验证原件、索引内容和依赖。校验失败返回损坏/等待状态，不返回虚构的就绪值。显式修复保存隔离证据并重新采集，但不能以新来源替换旧快照依赖。

事件分类重建存储契约（已实现并正式应用，独立保护终验PASS）：`data-rebuild-events`复用现有market索引、内容地址原件与event_batch，不另建行情库或并行规范化程序。默认计划模式完全只读、无网络；显式`--apply --expected-plan`才在market writer锁内检查当前计划并发布。范围仅指定代码的当前最新批次；晚于as-of的批次不得通过回退旧批次绕过，缺失/损坏/未知结构/成员失配或超出kind/status变化时全单拒绝。

仅分类变化事件追加新观测；每个新完整批次引用“未变化旧成员ID＋变化新成员ID”，所有受影响批次和事件同一SQLite事务提交。旧observations、来源原件、旧batch、冻结快照及计划引用全部保留；事务失败不能露出部分新批次。新事件保持原`raw_ref/provider/fetched_at`，`available_at`取实际修复时间，normalization metadata记录修订和previous observation ID；元数据是审计关系，不是另一套程序。旧知识截止继续使用旧事实，不能伪装成原采集时已存在的修正。正常命令2同日成功缓存复用、命令1准备权益再导入，同原件与相同当前解释须复用已存修正版及原available_at；已有原件确有新解释才按实际规范化时间追加，batch的available_at不得早于成员；真正新原件仍按fetched_at可用。fetched_at及coverage继续表达真实来源取得/覆盖时间，不把修正版回填到旧时间。

`event_coverage`不重建、不更新时间，原来源完整性及依赖未变；实际来源取得时间继续用于新鲜度。重建不能把缓存变成一次新采集。`requests/jobs`、价格、池、准备任务与账本不变；apply仍需携带expected-plan，但锁内当前已无变化时返回`UNCHANGED`且零新增，可重复此前已应用的plan_id；仍有变化而计划摘要不匹配时必须拒绝。源码修改本身不改变已存payload，须经上述受控步骤及用途复核，不能直接把旧记录改为READY。SC01—SC04的范围和验收见[数据层清单](data-layer-backlog.md)。

价格和金额用十进制字符串，成交量标准化为股、金额为元；未知字段保持未知并保留来源能力与精度。股票池和行情已经具备只代表这些事实可读，是否满足估值、策略、执行或权益用途由统一服务分别判断。

## Git 边界

提交核心源代码、核心默认配置/模板、数据库迁移、核心测试、脱敏方案、`pyproject.toml`、`uv.lock`、`.python-version`与`.env.example`。完整发布附加层有明确例外，见下段；公开方案保留其分层职责与运行契约，不代表该源码随Git分发。

不提交完整`src/china_stock_sim/publishing/`、`configs/publishing*.toml`（含模板）、整个`deploy/web/`、`deploy/launchd/bin/A股模拟-云端上传`及云端专用测试。也不提交`.venv`、`.cache`、真实行情、索引和快照、SQLite及WAL/SHM、验收原件、模型响应、日志、生成页面、备份、真实凭据/密钥、生成的`current.plist`、`.app`和`.build.json`。业务/首页两个便携启动脚本、原生菜单栏源码与核心安全模板保留；目录占位不代表已运行。排除Git不删除本机文件、不停用已有上传任务；完整迁移需另行保全该附加层及配置。公开说明不得写入真实服务器地址、个人路径或SSH/代理配置，本机附件和记录路径均不由公开Git提供。

本机 Git hooks 已启用；新克隆需`git config core.hooksPath .githooks`。提交前运行`.venv/bin/python scripts/check_secrets.py --worktree`覆盖已跟踪及未忽略未跟踪文件；用户暂存后用`--staged`检查索引，已有HEAD时另检查`--history HEAD`。`--tracked`不检查首次提交的未跟踪文件。路径保护会拒绝强制加入的本机配置/私钥/运行数据且不读取内容；凭据扫描仍有模式覆盖边界。忽略和扫描不替代密钥权限保护；CI必须在远端实际运行后才有相应结果，Git不承担运行数据灾备。

公开仓库准备的当前验收边界为GP01—GP09：GP01只读核对清理后运行；GP02忽略私有配置/数据/主机构建物；GP03核心安全模板且真实配置不变（发布/Web模板按LP边界仅本机）；GP04完整工作区候选扫描；GP05凭据内容及危险路径保护；GP06合成测试值保持语义；GP07只做必要回归及实际候选检查；GP08统一入口且不隐式提交/重装；GP09公开文档去除真实部署绑定。实现与发生时结果统一见[当前状态](implementation-status.md)和[GP验收](../logs/git-push-preflight-2026-11-05-final-acceptance.json)。

当前独立发布边界按LP01—LP06维护：LP01完整发布程序/配置/专用部署与测试仅留本机；LP02忽略及强制提交路径保护；LP03核心只通过可选命令和状态接口接入、已安装组件损坏不能冒充未安装；LP04混合测试保留核心覆盖；LP05本机完整环境与不含发布层的公开副本分别做必要验证；LP06统一文档、确认已安装任务匹配，必要时才重装。该边界取代此前GP阶段允许公开发布模板的范围；已封口GP事实不改写。当前结果见[实施状态](implementation-status.md)及[LP验收](../logs/local-publishing-boundary-2026-11-05-final-acceptance.json)。

## 维护约定

- 数据查询不联网、不建目录；采集、修复和冻结必须走明确写入口。
- 初始资金只记录一次；重复初始化不增加本金，不用删库处理异常。
- 不手改市场索引、冻结快照或既有账本，修订须保留来源和前后关联。
- 报告异常只重建报告，不能重跑交易来修复页面。
- 运行中的 SQLite 备份应使用备份接口，不能只复制单个数据库文件。
- 同盘保存不是异机容灾；本机关闭期间没有后台采集或交易程序运行。

命令、API、续跑和当前真实验证范围见[数据准备说明](first-preparation.md)；全量验收结果见[数据层清单](data-layer-backlog.md)。

## 当前状态与封口证据入口

[实施状态](implementation-status.md)是唯一当前人读汇总，[project-current-state.json](../logs/project-current-state.json)是可更新的机器派生快照。日期化采集、测试、requirements及最终验收记录保持发生时事实，其历史摘要不随现行文档或页面更新。

`data/normalized/project-audit-evidence/<sha>.evidence`保存被封口摘要引用的旧字节；对应审计解析清单记录原路径、摘要和保存位置。它仅用于重验历史收据，不作为程序、配置或另一版方案运行。新日期最终验收保存当时当前内容与摘要快照；不把可变当前路径作为永久固定字节依赖。

## 策略与账户结构（SL历史建设及现行schema4契约）

中立`common/strategy_definitions.py`负责显式注册定义、参数schema及明确源码文件的SHA摘要，不导入算法或执行计算；`strategies/base.py`保存PlanningContext/StrategyDecision，`features/rules/sizing.py`实现规则规划，`llm/llm_prompt/llm_call.py`实现模型输入/解析/硬截止，`strategies/cli.py`提供命令。`application/strategy_inputs.py`组装公开事实，`application/strategy.py`负责准备/规划/发布/恢复；实际写账仍只经账本接口。这属于现有策略与流程层，不新增业务层或服务。

2026-11-01的SL建设在`ledger.sqlite3`增加`account_policies`、`strategy_decisions`两表，当时生产schema2→3先备份再显式完成；现行schema4保留这两张权威表；该次迁移保留原19表逻辑摘要，仅新增3条base政策、0条决策；不能将迁移时计数当作后续运行状态。不可变参数、输入包及decision_core沿用`data/plans/`，Proposal/验证/封存原件按decision_id单向关联，避免摘要循环；运行JSONL沿用`logs/`，可读审计JSON/CSV放`reports/strategies/`。不建立第二个市场库或账户库，不复制历史行情。

所有新增账户经公开开户接口注册和唯一注资；配置仅提供种子默认值，页面/CLI查询注册表。看板显示账户实际本金、策略种类/算法修订/参数修订及模型名，历史行绑定当时记录，不被最新配置重标。现读端按持久登记动态查询，真实库仍为原三个账户，SD01已完成，SM01已实现并验收；SX01已实现并验收；SL01—SL04历史映射见[原设计矩阵](../logs/strategy-design-2026-10-30-requirements.json)；本轮ST/AC实现与SA验收见[当前状态](implementation-status.md)，不刷新旧日期矩阵。

SD01数据支持已完成：公开MarketDataReader.signal_prices及现有服务一致，15项新增消费者测试、当时全项目1278项及Ruff通过。详见[SD01验收](../logs/data-sd01-2026-10-30-verification.json)。命令1/2及历史缓存不变。SM01已实现并验收，其既有证据见[SM01验收](../logs/model-sm01-2026-10-30-verification.json)；SX01已实现并验收，当前入口见[SX01验收](../logs/execution-sx01-2026-10-30-verification.json)；SL01—SL04已实现并验收，三策略与完整账户业务已接通独立应用；整日编排已实现，WF验收已通过。

冻结计划保留provenance_mode、decision_ref和独立strategy_market_snapshot_ids/strategy_data_evidence；执行资格仍由data_evidence保存。MANUAL/STRATEGY表示输入来源，不是新旧程序。SX01继续核验政策快照、原件及提案内部一致性；SL02本次另提供权威政策/唯一决策索引及PROPOSED封存互锁，SL04负责源结构迁移和备份自身配置恢复。SL-C01—SL-C20及生产迁移/隔离恢复均已通过，证据见[本次验收](../logs/ledger-strategy-support-2026-11-01-verification.json)。

SI01报告新增：每账户公开审计JSON写reports/strategies/，页面CSV按同一账户快照生成；非默认账户文件名采用账户ID的SHA256安全名，避免路径与大小写碰撞。默认三账户文件名保持可读。SI01本身不增加数据库或表；本次SL02的权威政策和未封存决策通过独立公开API/CLI查询，不能把原冻结计划页面当全部新索引展示。

SI01契约与浏览器验收边界见[界面方案](interface-design.txt)及[日期索引](architecture-change-log.md)；SL政策/决策API与CLI的独立验收不扩大原浏览器通过范围。

signal_prices.issues的对象格式、观测ID保留及不重建/重下载边界，统一见[数据层消费者契约](data-layer-backlog.md)。

## 整日流程持久状态

运行观测现行契约见[runtime-observability-design.txt](runtime-observability-design.txt)；方案、实现和本轮自动化已通过，三任务实际记录已按PASS_FOR_OBSERVED_SCOPE核验；业务在途不等于终态完成，详见本节末的日期证据。既有决策原件与模型公开响应支持已记录决策的解释，以前没有的CPU/RSS和逐次上传性能不可补造。新增JSONL只放有界摘要及run/account/decision等关联，不复制市场或决策巨量原件。原三任务stdout/stderr、journal、首页不可变采样与上传最新状态继续保留，最新状态不能代替逐次历史。

运行明细30天/压缩归档180天按保护清单受控维护；源码审计对象和唯一金融证据不套用日志期限。journal当前索引与历史引用分离，查询只读当前必要范围，不靠删除金融历史解决增长。runtime-history和runtime-archive-verify只读；runtime-archive只归档运行资料，保留损坏/未闭合记录并明确采样边界。首页刷新后的archive_due独立非阻塞、每次最多一日期并有扫描/字节预算，按实际内容指纹追加归档；通过auto-state游标优先未建包日期，否则轮询历史日期，避免新日期长期排不到；不归档全市场或账本。原账本30份完整成功日备份及唯一引用证据规则保持，同盘运行归档不提供磁盘损坏后的恢复保证，也不承诺任意未来研究数据。

运行首页与发布独立分层：`scheduler/status.py`只读账本`ledger/monitoring.py`有界字段、日历、journal和进程身份；`reporting/run_home.py`纯渲染，`scheduler/dashboard.py`用独立锁发布。`reports/index.html`是唯一首页提交点，内嵌完整同代状态；`reports/dashboard-snapshots/<publication_id>.json`先写不可变采样。无本机跨文件状态轮询；这些运行采样不代表另一版程序。配置`configs/dashboard.toml`，锁`data/state/dashboard-refresh.lock`，日志`logs/dashboard-refresh.*.log`。

本机`src/china_stock_sim/publishing/`为唯一独立发布层，实现两页包、配置、传输、回执和上传调度；整个模块及附属配置、部署、专用测试不随Git分发。核心仅通过可选接口读取上传状态、注册命令，缺该层不妨碍核心运行；公开克隆不带云端能力。`data/state/publishing/state.json`按目标保存最后成功时点last_success_slot、真实成功时间及冷却；独立publishing.lock不锁业务。只读兼容旧schema1：日期型scheduled_for/last_success_date映射该日09:00，原件不改；新成功写last_success_slot，last_success_date仍为实际published_at的上海自然日。未完成最近时点时next_due_at为该已到时点，成功后指向下一个时点。兼容历史示例：当时11月2日14:32成功仅满足该日09槽，只读下一期限转为当日21:00，不因此重新上传或提前记21成功；这不是当前发布状态。新清单scheduled_for保存最近已到09/21规范时点并绑定published_at，纯导出可为null；旧日期清单保留原字节、只读解析为原09槽，不能拿旧09包恢复成21成功。`reports/cloud-preview/current`为显式导出，远端current为完整校验后的静态发布；只含两页和必要展示数据，完整审计、日志、源码、备份和密钥仍留本机。通用契约见[publishing-schedule-design.txt](publishing-schedule-design.txt)，具体运维说明`deploy/web/README.txt`只留本机，不随Git分发。

`application/workflow.py`统一编排，`workflow_data.py`与`workflow_ledger.py`分别只消费数据/账本公开接口；`scheduler/planning.py`纯规划、`state.py`管理`data/state/workflow/journal.json`、`workspace.py`共用写锁、`launchd.py`提供显式部署维护。journal保存审计尝试和当前任务/冷却，不是第三个财务库；业务提交、连续水位及必要workflow_status/revision_state仍由账本判定，完整report_snapshot仅显式使用。市场和账本原锁/事务保持，报告与日常备份成功按修订去重。详见[workflow设计](workflow-design.txt)。

## 2026-11-02运行记录验收与原件边界

[真实采样](../logs/runtime-observability-2026-11-02-runtime-live-sample.json)核验5条JSONL和4份身份清单的完整源码对象闭包；采样时三任务安装、启用、加载及配置匹配，首页/上传有END且退出0，workflow仍在途。运行中的日志可继续追加；回执SHA绑定保留的采样字节，不把可变路径当永久不变原件。可空字段null正常保留为未知/不适用；早先workflow/上传误计omitted的原日志不回写，后续首页/上传0 omitted与旧历史PARTIAL分开。上传事件保存target_id/release_id等安全引用，身份中的公开max_output_tokens只接受严格数值，不将凭据样式值写入。

[首档](../logs/runtime-observability-2026-11-02-first-archive.json)1条日志/143文件COMPLETE、[verify READY](../logs/runtime-observability-2026-11-02-first-archive-verify.json)；[后档](../logs/runtime-observability-2026-11-02-live-archive.json)5条日志/156文件含1个在途前缀，[归档校验](../logs/runtime-observability-2026-11-02-live-archive-verify.json)同为PARTIAL，属于明确采样边界且无摘要损坏。首页自动归档分支实测SKIPPED/NO_PREVIOUS_RUNTIME_LOGS，不能冒充已经自动生成历史归档；上述真实包由显式runtime-archive创建。CLI对PARTIAL/BUSY/WAITING_MANUAL保留状态并退出2；追加日志后可另存新内容指纹包，原包和源文件不删。

[保护审计](../logs/runtime-observability-2026-11-02-protection.json)确认金融6表摘要和7项计数、240份原件、7份TOML、3份旧closeout保持；已有数据无需重新下载。[验证汇总](../logs/runtime-observability-2026-11-02-validation.json)记录2452项全量和最后355项相关测试、2490项去重并集，最终235项源码、测试、配置与入口清单与最后专项绑定，不能当作最后字节全量重测。

## schema4、共享对象与受控维护

同一`data/state/ledger.sqlite3`保存21表及有界经济索引，普通读取不透明展开历史巨型记录。`data/plans/`保留原逻辑path/content_hash；较大文档可用`cas-json-v1` wrapper指向`data/normalized/evidence-objects/<前两位>/<原raw SHA>.json.zlib`。原字节SHA、压缩stored SHA、大小和encoding分别记录，wrapper物理SHA不能当原raw SHA。`read_business_document`与`document_closure`统一解析逻辑原件及实际依赖；CAS显式大对象流式验证，不靠复制展开去重。

`plans`是`frozen-plan-header-v1`小索引，`strategy_decisions.proposal`是既有core提案引用；原完整查询显式resolve。`original_record_ref`保存旧行原始字节，`account_view_evidence`保存有界执行见证；日结`record_reference.snapshot`绑定当时可变订单快照。备份闭包归档两类opaque原件而不展开，所有权威业务文档根及其他普通CAS仍递归核验。新库/显式子迁移创建日期/状态查询索引，读查询不建索引。

`data/state/verification/`仅为按内容、核验程序和依赖文件身份绑定的可重建证明，缺失重新核验，不能成为金融事实。`data/state/workflow/journal.json`为storage_version2小索引，完整run/task由内容地址对象引用；read_current不展开全历史。具体文件布局由公开journal管理，不从私有对象文件猜当前业务状态。

`data/backups/ledger/`仍保留最近30份COMPLETE日恢复点及单列manual/migration；每份manifest包含自身配置、DB、全部实际依赖。不可变CAS/原件核验后可hardlink共享，DB/配置不硬链，恢复独立复制。`data/backups/runtime/`保存运行归档；`configs/runtime_retention.toml`默认30天原始明细/180天压缩归档、10MiB×5软轮换，保护引用优先。

`data/normalized/project-audit-evidence/<SHA>.evidence`可保留raw或原子换为审计wrapper；历史日期收据不改写。用`common.audit_archive.iter_evidence/read_evidence/verify_evidence`按旧SHA还原/验证，不直接对wrapper内容求hash冒称旧证据不符。该目录是历史审计解析材料，绝不作为并行运行版本。

`storage_cleanup.py`默认只给计划；`storage_promote.py`prepare绑定隔离迁移/计划子迁移收据，apply/rollback须审阅plan及维护窗口声明。只有清单明确且引用/身份复核通过才清理或切换。操作入口见README；实际切换、释放量和恢复运行以本轮收据为准。市场库、命令1/2、09/21上传与金融规则不因本次维护改变，不需要重新下载已有数据。

## 权益覆盖证明复用（CR04）

仅同 record_key 最新完整观测、payload、refreshed_as_of、provider/adapter 与依赖原件列表精确一致，且原件完整性及当前新鲜度有效时，复用原覆盖证明与可用时间。新的真实来源即使数值相同仍保存新证据；不倒填时间、不回退旧等值版本、不删除历史。普通损坏拒绝复用；显式 repair 且两源本轮新成功、完整性/新鲜度通过，可追加新证明，旧坏证明不覆写。账本按经济语义决定有无必要新账单，来源 fingerprint 身份核验不放宽。详见现行存储运行方案CR04；18:10新鲜度守卫与命令1/2不变。


<a id="DO01-DO10"></a>

## 目录整理与唯一现行内容（DO01—DO10，2026-11-05）

- DO01 本轮明确授权范围是 `<WORKSPACE_ROOT>` 全目录。主项目仍为其唯一运行根 `simulate-china-stocks`；先盘点外层材料与主项目的用途、依赖、引用、打开句柄、符号链接和环境入口，再审阅删除清单。不执行外层退役程序，不导入旧账户、回测和采集缓存。
- DO02 外层只有确实补足现行项目缺项的内容才整合到对应层，并更新入口、调用或测试证明用途。已由主项目承接或与现行规则冲突的原型材料不机械搬入；本轮材料审查未发现独有现行缺项，故无需制造迁入副本。
- DO03 初次整理采用经核验清单移入系统废纸篓，脱离工作目录并保留撤销能力，不隐式永久清空。后续只有用户明确授权具体目录时才核对身份并精确永久删除，另存不可撤销操作回执，不扩大到其他废纸篓内容。逐项核对路径归属、原inode/设备、目标不存在、非主项目祖先或链接，再原子移动；不跟随符号链接、不使用广泛通配rm。不承诺移动废纸篓已经释放磁盘空间。
- DO04 主项目保留源程序、配置、唯一运行环境、分层设计、测试、运维脚本、部署产物和实际数据。主库、冻结证据、CAS对象、原始来源、维护恢复基线和有引用的审计/报告/备份均有业务或追溯用途，不按日期直接当作旧版本删除；其留存按SG/RO保护闭包与既定期限。
- DO05 无业务作用的.DS_Store及可再生开发缓存（.pytest_cache、.ruff_cache、源码/测试__pycache__等）可纳入清单；不删除.venv依赖或构建好的原生应用，不扩展数据迁移、证据压缩或程序性能优化。缓存自然再生不表示另一版本。
- DO06 README归纳为运行使用说明，AGENTS归纳为工作规则，implementation-status仅保留当前能力/已核运行范围/未决事项/证据入口。架构总览只定义层次、边界与链接，各分层附件保留唯一详细契约；历史建设结果收敛为日期化决策和验收索引，删除重复长篇叙述及失效入口，不把历史事实改写成今天。
- DO07 改写被封口SHA引用的现行文档前保存原字节至project-audit-evidence，已存在压缩wrapper不覆盖。原日期日志/验收收据及金融原件不改写。文档清理不删除未解决问题或扩大验收范围，不改变交易、模型、账本和调度规则。
- DO08 删除前进行独立材料与依赖审查；删除后一次必要验证路径、安装环境、命令帮助、现行文档链接与源码/配置/策略身份保护、后台状态。仅文档和退役材料/缓存变化不运行完整测试套件、不触发金融业务；有新发现才定点追加核验。
- DO09 若确有程序/启动参数或公开页面变化，按对应方案安全重装原三个任务或发布云端；本轮目标是整理目录内容，判定无需变化时保持运行部署，避免为“整理”无意义地重启或重复上传。
- DO10 最终保存外层和主项目的用途分组、处置清单、删除/整合结果及必要验证回执，两个当前入口统一到本轮结果。删除后父目录仅保留主项目；当前设计/程序只一套，废纸篓不是项目运行目录，历史证据不是平行版本。

历史实施、测试、安装及云端部署的发生时事实统一见 docs/architecture-change-log.md 的日期化证据索引；本附件只定义当前契约，不重复维护历史运行快照。
