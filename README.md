# 中国股票前向模拟

在 Mac 上运行的沪深主板 A 股前向模拟项目：提前冻结计划，使用随后取得的真实行情模拟执行，记录现金、持仓、费用和盈亏。唯一运行根是当前 `simulate-china-stocks` 目录；下列相对路径及命令均以此为根，`/path/to/simulate-china-stocks` 仅为跨机器示例。

[当前建设与运行状态](docs/implementation-status.md) · [机器状态](logs/project-current-state.json) · [本机运行首页](reports/index.html) · [本机交易看板](reports/forward_current/index.html)

公开 Git 仓库提供采集、策略、执行、账本、业务调度、本机首页/交易看板和菜单栏显示的核心程序。独立云端发布层 `src/china_stock_sim/publishing/`、其配置及模板、Web 部署目录、上传启动器和专用测试只保留本机，不随 Git 分发。公开克隆无需这些附加文件即可使用核心功能；本机现有三任务继续保留。数据、日志、生成页和真实部署信息也不提交，上述本机链接仅在已有运行环境有效。实际云端地址查本机 `logs/project-current-state.json` 的 `production_runtime.cloud_url`；本机运维说明 `deploy/web/README.txt` 同样不由 Git 提供。

新会话先读当前状态，再读[总架构](docs/architecture.txt)及相关分层附件。方案、程序和配置只维护一套现行内容；[建设决策与验收索引](docs/architecture-change-log.md)及封口记录保留发生时事实，不作为另一套运行版本。数据用途就绪、计划冻结、实际成交和最终日结是不同状态，不能互相替代。

## 日常使用与后台任务

三个账户 S1、S1-LT、LLM 独立运行，不合并资产。新账户种子本金各50,000元；已有账户沿用原本金、政策、观察起点和交易历史。S1、S1-LT没有每日股票代码数量截断；LLM每日买卖意图合计最多5个代码。每日有计划不保证每日有成交，限价、T+1、资金及来源就绪规则仍生效。

| 后台任务 | 命令与职责 | 现行触发 |
|---|---|---|
| A股模拟-业务调度 | `workflow-run --download --scheduled`：数据、权益、原单执行、日结、未来计划、报告和备份 | 登录加载；每小时00/15/30/45分及18:10日历；17:00—19:00跳过自动业务 |
| A股模拟-首页刷新 | `dashboard-refresh --scheduled`：只读采样首页并执行有界运行日志维护 | 登录加载；每小时00/15/30/45分日历 |
| A股模拟-云端上传 | `dashboard-sync --scheduled`：仅发布静态展示包 | 登录加载及每小时四刻补检；上海09:00/21:00应用槽去重 |

此表记录包含本机发布附加组件的完整运行环境；公开克隆默认提供前两项，上传命令仅在本机附加组件存在时注册。三项共用本项目Python，Label、命令和日志独立，按次结束，无KeepAlive。命名入口只`exec`原解释器，不增加业务进程。[部署说明](deploy/launchd/README.txt)记录安装、命名和归属保护。

日历触发使用`StartCalendarInterval`，不再使用`StartInterval`。睡眠跨过一个或多个定时点，唤醒后合并检查当前状态，不逐次回放错过的轮次；短睡未跨点或仅解锁不保证立即启动。已有同Label进程继续原轮，不中断、不并行另开：10:15启动、10:30仍在运行、10:38结束，则下一正常刻度为10:45。`next_check_at`表示计划刻度，不承诺精确到秒。开机登录由`RunAtLoad`安排检查；真实未完成业务按持久状态续办。

日常顺序：交易日18:10达到数据门槛 → 自动业务19:00后检查 → 补齐到期数据和旧到新账务 → 按原冻结计划模拟执行、日结 → 生成并冻结下一真实交易日计划 → 报告及日备份。下一交易日09:00是计划截止；缺失计划的历史日期不能补造交易，已有计划可延后采集和结算，持仓继续保留。只有LLM策略规划调用模型，其余调度、采集、检查、记账、恢复和绿灯判断均由普通程序自主执行。

## 晚上何时可以关机

菜单栏绿色 **A股·可关机** 表示核查时：D日三账户账务与最终估值连续完成，N下一真实交易日三账户合法计划已冻结，必要义务无未决，相关财务写入不在途。合法空计划也计入完成；正常关机不再依赖早晨09:00前补规划。报告、首页和云端上传不参与判绿。

黄色表示未完成，红色表示明确阻塞，灰色表示待晚间、待核查或已失效。点击查看D/N、账户、原因、检查时间及“立即检查”；每30秒只读刷新，睡眠/唤醒会清除旧绿并复查，不自动关机。交易日09:00—18:10显示待晚间，非交易日按真实交易日历判D/N。菜单栏是额外一个轻量常驻显示进程，退出它不停止原三个任务。完整规则见[关机就绪方案](docs/evening-readiness-design.txt)。
```sh
./.venv/bin/china-stock-sim shutdown-status
./.venv/bin/china-stock-sim shutdown-indicator-build
./.venv/bin/china-stock-sim shutdown-indicator-install
./.venv/bin/china-stock-sim shutdown-indicator-status
./.venv/bin/china-stock-sim shutdown-indicator-uninstall
```
`shutdown-status`只读；build/install/uninstall仅显式维护显示程序。

## 交易看板怎么用

1. **资产与盈亏**：先看账务所属日、总资产、当日/累计盈亏及现金、预留、可用资金。页面更新时间不等于估值日期。
2. **选择交易日**：联动该日计划、实际成交、参考/目标日行情和个股当日损益；未来或未日结日显示等待，不借用最近日。未成交和“已过09:00截止”显示所属日期及原因，已有冻结计划仍有效。
3. **当前持仓**：固定对应最新账单估值日。含费成本均价＝剩余含费成本÷剩余股数；展开可看仍有剩余股份的买入/权益批次、原成交价、费用和剩余成本。部分卖出按账本FIFO分配；耗尽批次仍保留在完整历史。
4. **完整买卖与已清仓汇总**：按股票/持仓状态筛选，汇总与逐笔各20行一页；全部已记录成交可导出CSV。股份清仓不等于权益税款结束，回购不抹掉旧历史。累计交易已实现不冒称含分红、红利税的终身总收益。
5. **资金图及审计**：资产/现金/持仓市值与累计盈亏分图，标明日期、人民币元、图例及数值；缺估值断线。账单、现金、权益税款、运行及冻结审计按需展开。

个股当日损益以本日市值减前一交易日市值、加当日成交净现金计算，费用已包含。缺连续最终估值、权益税款归属或成本更正证据时显示待核，不填0或分摊账户差额。历史日期展示范围为近期60个业务交易日及未决关联；完整成交历史独立覆盖全部已记录成交，不为此新增行情采集。详细口径见[界面方案](docs/interface-design.txt)和[账本投影](docs/ledger-design.txt)。

浏览器每60秒重载已有快照；首页每15分钟采样，交易报告由业务阶段产出。超过30分钟未采样提示首页滞后。双击`前向模拟.command`仅刷新并打开首页，不初始化、不采集、不交易。

## 本机首页与独立云端发布层

核心仓库的本机首页与交易看板独立可用：
```sh
./.venv/bin/china-stock-sim dashboard-status
./.venv/bin/china-stock-sim dashboard-refresh
./.venv/bin/china-stock-sim dashboard-service-status
./.venv/bin/china-stock-sim dashboard-install
./.venv/bin/china-stock-sim dashboard-uninstall
```
`dashboard-status`与`dashboard-service-status`分别查询首页投影和刷新服务；`dashboard-refresh`只写本机首页。交易页由既有账本报告生成，刷新首页不代替重建交易页。

本机唯一发布层 `src/china_stock_sim/publishing/` 独立承担两页白名单包构建、校验、导出、SSH传输、回执、09:00/21:00时点去重和上传LaunchAgent维护。它读取独立本机配置，不触发采集、策略或账本；主程序通过可选接口接入，不复制另一套发布实现。缺少该层时，核心CLI、业务调度、本机页面和菜单栏照常可用，首页将上传能力标为未安装；本机已有该层及配置，原上传任务继续使用。

以下仅描述本机附加组件的操作契约，公开克隆不含对应程序、配置、Web部署材料或专用测试：`dashboard-export`导出两页及必要JSON/CSV/CSS/JS，`dashboard-sync-status`查询上传，`dashboard-sync-install`/`dashboard-sync-uninstall`管理上传任务；明确页面维护可用`dashboard-sync --republish`。`--republish`与`--scheduled`互斥，保留锁、冷却、完整性和原子切换保护。修改交易页后先由只读报告投影重建，再刷新首页、导出和发布，不用业务重跑代替展示维护。本机操作细节在不提交Git的`deploy/web/README.txt`；通用契约见[发布方案](docs/publishing-schedule-design.txt)。

上海09:00/21:00每天生效，不限交易日；按目标与最近已到槽去重。错过多个槽只上传恢复时最新状态一次，凌晨可补前日21槽。同槽成功不重复，失败保留上个完整包并遵守冷却；调用跨过时点也不把旧包冒充新槽。Ubuntu只服务已上传静态页和必要展示数据，不承载业务程序、数据库、模型、凭据或完整私有审计。

## 环境与账户初始化
```sh
./scripts/setup.sh
./.venv/bin/china-stock-sim doctor
./.venv/bin/china-stock-sim status
./.venv/bin/china-stock-sim report
```
`setup.sh`使用Python3.12、`uv.lock`和项目`.venv`，同步依赖后初始化并诊断。已有环境可`uv sync --locked --group dev`；`china-stock-sim init`只核验已有账本，不再注资或清空历史。`doctor`不联网、不创建缺失库；`report`从已有账本重建页面，不运行买卖。模型密钥不是初始化账户的前提。

## 账户查询与历史策略审计
```sh
./.venv/bin/china-stock-sim account-list
./.venv/bin/china-stock-sim account-profile --account LLM
./.venv/bin/china-stock-sim account-audit --account LLM
./.venv/bin/china-stock-sim account-audit --account LLM --output reports/strategies/LLM-audit.json
# YYYY-MM-DD 替换为要查询的实际目标日。
./.venv/bin/china-stock-sim account-policy --account LLM --session YYYY-MM-DD
./.venv/bin/china-stock-sim strategy-decision --account LLM --session YYYY-MM-DD
```
查询不采集、不调用模型、不记账；`account-audit --output`才显式写报告。账户来自持久登记，省略时默认LLM，显式未知账户报错。历史身份由原冻结证据核验，缺失标明不可用；未记录决策返回NOT_FOUND。配置accounts只负责空库种子，修改TOML不会增删现有账户。

## 显式开户与未来政策

以下是操作格式，须按实际需求填写项目内JSON；查看文档不会执行。开户示例保存为`data/plans/account-create.json`：
```json
{
  "account_id": "ALT-S1",
  "display_name": "S1独立账户",
  "initial_cash_fen": 3000000,
  "strategy_id": "S1",
  "strategy_parameters": {"target_count": 8, "target_weight": "0.095"}
}
```
```sh
./.venv/bin/china-stock-sim account-create \
  --input data/plans/account-create.json --operation-id create-alt-s1-001
```
本金为正整数分，比例为十进制字符串。开户原子保存账户、唯一注资、状态和base政策，重放不重复注资；不自动激活、调用模型或规划。未来政策示例保存为`data/plans/account-policy.json`，日期须是已知且尚未到09:00截止的未来交易日：
```json
{
  "effective_session": "YYYY-MM-DD",
  "strategy_parameters": {"target_count": 6},
  "reason": "调整后续目标持仓数量"
}
```
```sh
./.venv/bin/china-stock-sim account-profile --account ALT-S1
./.venv/bin/china-stock-sim account-policy-set --account ALT-S1 \
  --input data/plans/account-policy.json --operation-id policy-alt-s1-001 \
  --expected-revision ACCOUNT_REVISION
```
`ACCOUNT_REVISION`替换为刚读取的财务修订。政策不可回写；未决订单、活动父任务或生效日后已有决策/尝试/计划会触发互锁。开户、政策和激活分别留回执。决策API `prepare_strategy_decision/save_strategy_decision/get_strategy_decision/list_strategy_decisions`的DTO与`expected_sequence`约束见[账本方案](docs/ledger-design.txt)；前两者仅登记输入/结果，不计算策略或请求模型。

## 独立策略规划、发布与恢复

`strategy-inspect`只读；`strategy-plan`可能保存输入及调用LLM，形成PROPOSED；`strategy-publish`完整验证并冻结；`strategy-recover`复用已存响应/提案。都不采集、不撮合/结算、不开户或激活。`YYYY-MM-DD`须是已知未来目标日，且在当日上海09:00前执行。
```sh
./.venv/bin/china-stock-sim strategy-inspect --account S1 --target-session YYYY-MM-DD
./.venv/bin/china-stock-sim strategy-inspect --account LLM --target-session YYYY-MM-DD --include-input
./.venv/bin/china-stock-sim strategy-plan --account S1 --target-session YYYY-MM-DD
./.venv/bin/china-stock-sim strategy-publish --account S1 --target-session YYYY-MM-DD
./.venv/bin/china-stock-sim strategy-recover --account S1 --target-session YYYY-MM-DD
./.venv/bin/china-stock-sim strategy-evaluate --input data/plans/fixed-input.json
./.venv/bin/china-stock-sim strategy-evaluate --input data/plans/fixed-input.json --response data/plans/model-response.json
./.venv/bin/china-stock-sim strategy-audit --account S1 --output reports/strategies/S1-decisions.json
./.venv/bin/china-stock-sim strategy-audit --strategy-id S1 --format csv --output reports/strategies/S1-decisions.csv
```
`strategy-evaluate`只解析固定输入包；LLM必须同时提供保存响应，不请求模型。`--known-at`固定知识截止；审计可按`--start/--end/--strategy-version/--policy-id`筛选，只有显式`--output`写`reports/strategies/`，CSV仅供audit。公开应用入口为`StrategyApplication.inspect/prepare/evaluate/plan/publish/recover/audit`。

未激活账户需先绑定当前实现政策，再显式激活；已有三账户不重复激活、开户或注资。下面仅用于尚未激活账户：
```sh
# data/plans/activate.json内容：{"target_session":"YYYY-MM-DD"}
./.venv/bin/china-stock-sim ledger-apply --action activate_simulation --account S1 \
  --input data/plans/activate.json --operation-id activate-s1-001 --expected-revision ACCOUNT_REVISION
```
旧政策、计划和父任务保留。实现变化不能靠重复init或改TOML采用；`CONFIG_CHANGED/FUTURE_POLICY_UPDATE_REQUIRED`须按未来政策处理。同参数实现维护仅允许[策略方案](docs/strategy-design.txt)与[账本方案](docs/ledger-design.txt)规定的`IMPLEMENTATION_MAINTENANCE`白名单和完整父任务衔接：transition恰含kind、最新from_policy_id/from_policy_hash，另给未来effective_session、理由、稳定operation-id及expected-revision，不能借此改算法、模型、提示词或延期。

模型默认输出上限2048token；每账户/目标日最多两次持久尝试，共用首次占位起算的默认120秒硬时限，且不晚于08:59:50。重启不重置次数，已保存响应先恢复，不重问模型。

## 独立数据层与命令1、命令2

数据层不要求开户，不读取模型密钥，不调用策略、撮合或账本。独立入口`python -m china_stock_sim.market`与总入口共用数据系统。以下日期均为占位，实际采集选择已到18:10门槛的业务日：

```sh
./.venv/bin/python -m china_stock_sim.market data-doctor
./.venv/bin/python -m china_stock_sim.market data-sync --as-of YYYY-MM-DD --codes SH600000,SZ000001
./.venv/bin/python -m china_stock_sim.market data-sync --as-of YYYY-MM-DD --codes SH600000,SZ000001 --download
./.venv/bin/python -m china_stock_sim.market data-query --code SH600000 --as-of YYYY-MM-DD --purpose rules
./.venv/bin/china-stock-sim prepare-data --as-of YYYY-MM-DD
./.venv/bin/python -m china_stock_sim.market data-status
```

**命令1 `prepare-data`**：首次/月末按最近20日成交额选最多100只候选，准备121日历史。**命令2 `data-sync`**：日常补候选、持仓、挂单、未结权益及指数；出池持仓继续维护。默认只检查缓存，`--download`才授权网络；`--purpose llm`不调用模型。

月末由真实交易日历判断：先命令2维护D日旧池 → 命令1换池 → 如新池有缺口，再以命令2显式`--codes`补齐，仍截至D，新池下一交易日生效。已发布新池不因补缺失败回退重选。`data-status`是采集进度，按用途能否使用由`data-query`判断；个别异常隔离，来源冲突保留，未知不当作0。完整CLI/API、冷却、覆盖和来源规则见[数据准备说明](docs/first-preparation.md)及[数据层清单](docs/data-layer-backlog.md)。

离线事件分类维护仅用已有原件，默认返回计划；应用须锁内匹配计划摘要，不联网、不改变来源新鲜度，不覆写旧批次：
```sh
./.venv/bin/python -m china_stock_sim.market data-rebuild-events \
  --as-of YYYY-MM-DD --codes SH601166,SZ000063
./.venv/bin/python -m china_stock_sim.market data-rebuild-events \
  --as-of YYYY-MM-DD --codes SH601166,SZ000063 \
  --apply --expected-plan PLAN_SHA256
```
晚于as-of的当前批次、缺损原件、未知结构或越界变化整单拒绝；相同结果返回UNCHANGED。详细范围见[事件分类离线重建](docs/first-preparation.md#事件分类离线重建)。跨目录调用时使用目标项目解释器和显式root：
```sh
/path/to/simulate-china-stocks/.venv/bin/python -m china_stock_sim.market \
  --root /path/to/simulate-china-stocks data-doctor
```
BaoStock提供日历/证券/行情及权益线索，腾讯提供备用价格与三个指数，东方财富提供补充权益。来源缺项保持未知，有限重试和备用源不保证每次成功；原件和用途限制保留。数据可随本项目迁移，无外层旧工程运行依赖。

## 整日流程与自动触发
```bash
.venv/bin/china-stock-sim workflow-inspect
.venv/bin/china-stock-sim workflow-run
.venv/bin/china-stock-sim workflow-run --download
.venv/bin/china-stock-sim scheduler-status
.venv/bin/china-stock-sim scheduler-render
```
`workflow-inspect`、`scheduler-status`只读；`workflow-run`即使不带`--download`也可能记账、规划和备份，只是不新增网络采集。自动入口额外带`--scheduled`并遵守enabled和避让。`scheduler-install`/`scheduler-uninstall`显式管理业务任务；`scheduler-render --output deploy/launchd/文件.plist`只写预览，不安装。

默认每轮最多20个交易日、3600秒阶段预算；已开始的原子步骤按既有边界完成，不强杀。冷却、Retry-After、模型首次硬截止保持；单户失败隔离，报告/备份按修订去重。恢复按真实日历及旧到新待办推进，不提供历史规划入口。迟到现金权益或历史价格更正可触发必要重述，纯引用/编号/覆盖变化不制造新账单；保留原成交和证据。完整顺序见[流程方案](docs/workflow-design.txt)。

## 独立账本与执行命令
```sh
# 只读查看账本；程序支持schema4，查询不会自动迁移或启用业务。
./.venv/bin/china-stock-sim status
./.venv/bin/china-stock-sim ledger-status --account LLM
./.venv/bin/china-stock-sim ledger-query --account LLM --kind history
./.venv/bin/china-stock-sim ledger-audit
```
程序新建schema4、同一21表账本，普通查询不迁移、不采集、不调用模型。`ledger-apply`接受完整JSON、操作ID和期望修订，不替策略生成交易。`ledger-migrate`保留源1/2兼容；源3须按隔离compact/受控切换流程处理，不重新初始化政策。

`ledger-backup --label LABEL`的LABEL只允许daily/manual/migration；备份数据库、递归引用及自身非密钥规则配置，不含.env。`ledger-restore --backup-id ID --target-root EMPTY_DIR`只恢复到新的空隔离目录，不用今天配置补缺；市场索引有独立恢复边界。连续不完整备份不能挤掉最后完整备份。

税务起点与权益身份通过`ledger-apply --action resolve_tax_holding_rule/resolve_event_identity`明确维护；`configs/tax_holding_rules.toml`默认空规则，缺依据保持UNKNOWN/RULE_PENDING。执行层纯查询为：

```sh
./.venv/bin/python -m china_stock_sim.execution execution-validate --input data/plans/proposal.json
./.venv/bin/python -m china_stock_sim.execution execution-match --account LLM --plan-id PLAN_ID
```

输入/ID来自实际意图和封存计划。显式变更入口为`ExecutionApplication.freeze/settle`；契约见[执行方案](docs/execution-design.txt)。

## 运行日志、审计与受控清理

三任务逐次写`logs/runtime/YYYY-MM-DD/<invocation_id>.jsonl`，记录START、阶段、END、时间、结果、源码/配置身份和业务引用；成功、失败、忙碌、冷却和空操作都可追溯。耗时为单调时钟，CPU为增量，RSS为进程终生峰值；未知不填0。异常保留安全堆栈，不记录密钥。硬中断可缺END，按PID及出生身份判断；Python启动前失败还需看stderr和launchd状态。
```sh
./.venv/bin/china-stock-sim runtime-history --date YYYY-MM-DD --task workflow --limit 20
./.venv/bin/china-stock-sim runtime-archive --date YYYY-MM-DD
./.venv/bin/china-stock-sim runtime-archive-verify --archive ARCHIVE_PATH
```
`runtime-history`只读，可按task/invocation-id/run-id/account-id/decision-id/limit筛选。`runtime-archive`写带摘要清单的归档；verify只读，不覆盖项目。PARTIAL/BUSY/WAITING_MANUAL退出2须结合原因，不一概视为损坏。首页任务有界推进归档与留存，不另设后台任务。

运行明细默认30天、压缩归档180天；日备份保留最近30份完整成功daily，manual/migration单列。未解决异常、在途/身份不明进程、金融证据及封口引用优先保护；没有完整保护清单不删除。stdout软轮换10MiB×5仅在相应任务停机时执行，同盘备份不是异机灾备。记录/归档故障不改变财务结果。详见[运行观测](docs/runtime-observability-design.txt)及[存储治理](docs/storage-runtime-governance-design.txt)。

以下维护先生成并审核计划；占位路径须替换为实际回执，apply/rollback只在明确停写窗口执行：
```sh
# 默认零删除计划；校验某份历史原字节SHA（支持已压缩.evidence）。
.venv/bin/python scripts/storage_cleanup.py
.venv/bin/python scripts/storage_cleanup.py --verify-evidence OLD_SHA256
# 按已核验计划选择明确组件；不能直接按日期或目录删除。
.venv/bin/python scripts/storage_cleanup.py --plan logs/REVIEWED_PLAN.json --apply audit backups cache
# 先绑定隔离迁移与计划/决策子迁移回执，再审核输出计划。
.venv/bin/python scripts/storage_promote.py prepare --staging-receipt logs/STAGING.json --projection-receipt logs/PROJECTION.json
.venv/bin/python scripts/storage_promote.py apply --plan logs/PROMOTION_PLAN.json --maintenance-confirmed
# 仅明确回退时使用同一计划。
.venv/bin/python scripts/storage_promote.py rollback --plan logs/PROMOTION_PLAN.json --maintenance-confirmed
.venv/bin/china-stock-sim runtime-retention-plan --protection-manifest logs/PROTECTION.json
.venv/bin/china-stock-sim runtime-retention-apply --protection-manifest logs/PROTECTION.json --expected-plan PLAN_ID
.venv/bin/china-stock-sim runtime-log-rotate --stopped-task workflow --stopped-task homepage --stopped-task upload
```
`.evidence`可能是raw或压缩wrapper，使用`common.audit_archive.iter_evidence/read_evidence/verify_evidence`按原SHA解析，不能用wrapper物理摘要冒充原文。冻结计划及原始经济事实、源码审计身份和恢复点依赖不套用普通缓存清理。生产展示使用recent60与未决投影；完整交易历史是同事务窄经济投影，完整冻结审计通过full/materialize显式读取，不能把近期摘要冒称全部历史。

## 项目结构与分层

| 路径/层 | 用途 |
|---|---|
| `src/china_stock_sim/market` | 来源适配、标准化、日历/权益/候选池、增量、查询与快照 |
| `strategies`、`models` | 三策略逻辑；独立可替换模型适配与脱敏审计 |
| `execution`、`ledger` | 纯验证/撮合；账户、现金、持仓、权益、日结、政策、决策和备份 |
| `application`、`scheduler` | 业务编排/封存/恢复；纯日程、耐久状态、工作区锁及launchd |
| `reporting` | 核心仓库中的本机只读页面 |
| `publishing`（仅本机，不随Git分发） | 独立静态包、传输、回执及上传调度；核心通过可选接口接入 |
| `readiness`、`desktop` | 关机就绪只读判定；原生菜单栏显示及安装 |
| `common` | 中立契约、格式、费用、证据与共享校验 |
| `configs`、`.env` | 核心通用配置及`.env.example`可提交；发布配置/模板与真实模型凭据仅本机保留 |
| `docs`、`tests`、`scripts`、`deploy` | 分层方案、核心测试与维护、本机部署；Web目录、上传启动器及专用云测试不随Git分发 |
| `data/state` | market.sqlite3与ledger.sqlite3、工作流/发布状态 |
| `data/raw`、`data/normalized`、`data/plans`、`data/backups` | 来源、规范化索引/CAS/审计证据、业务原件、恢复资料 |
| `reports`、`logs` | 可重建页面和导出；运行/验收/维护记录 |

[数据采集核验页](reports/data-sync/index.html)展示其生成时点的用途覆盖；[准备进度页](reports/preparation/index.html)展示最近首次/月末任务，二者都不是交易流水。[程序与数据目录](docs/data-layout.md)定义每类文件职责、引用和清理边界。

## 模型与必要验证

模型引用为daily_planner，当前默认qwen3.8-flash，配置见`configs/models.toml`；服务地址和密钥仅在本机.env或环境变量。无需改变模型即可运行其他层。
```sh
# 仅检查配置，不联网。
./.venv/bin/china-stock-sim model-check

# 显式发送一次非敏感测试提示，不自动重试。
./.venv/bin/china-stock-sim model-check --live
```
普通model-check不联网；`--live`显式发送一次非敏感请求且不自动重试，可能计费。连接成功不等于策略已运行；替换模型按[模型层说明](docs/model-layer.md)验证。

按变更范围选择必要验证，不为文档整理重复业务测试或真实模型调用。全项目检查命令为：
```sh
./.venv/bin/python -m pytest -q
./.venv/bin/ruff check .
./.venv/bin/python scripts/check_secrets.py --worktree
```
实际通过范围和日期见当前状态及封口回执，不把以前全量结果当作当前代码已重测。Git hooks/CI已提供，本地通过不代表已推送或远端CI执行。行情、数据库、业务/验收原件、生成页及凭据保留本机，不提交Git。

## 本机配置与公开仓库

- 模型凭据：项目根的隐藏文件 `.env` 保存服务地址、`CHINA_STOCK_SIM_LLM_API_KEY` 等实际值；`configs/models.toml` 只声明模型及环境变量名。加载时进程环境变量优先，随后读取 `.env`。公开仓库仅提交 `.env.example`；真实 `.env` 权限保持0600。
- 云端附加组件：`src/china_stock_sim/publishing/`及其专用测试、`configs/publishing*.toml`（包括模板）、`deploy/web/`整个目录、`deploy/launchd/bin/A股模拟-云端上传`均只留本机，不提交Git。公开克隆不承诺提供安装包或配置模板；迁移完整运行环境需单独保全这些本机文件。
- 云端连接：现有本机`configs/publishing.toml`保存目标引用，SSH主机、密钥路径和代理由`~/.ssh/config`管理，程序不硬编码。自动上传使用SSH密钥，不要求把服务器密码写入项目。真实配置及本机模板均不公开，不覆盖已正常使用的配置；文档中的`example.invalid:8080`仅为示例。
- `.gitignore`除上述完整发布附加层外，还排除真实凭据/私钥、行情/账本、日志/报告/备份、主机绑定的plist及构建应用；核心源码、业务/首页两个相对路径启动脚本、原生菜单栏源码和核心安全模板保留。Git不是运行数据备份，迁移机器须单独迁移受保护数据和本机配置。

提交前先运行 `scripts/check_secrets.py --worktree`，覆盖已跟踪和未忽略的未跟踪文件；空索引也会检查实际候选内容。用户暂存后可用 `--staged` 核对索引；已有提交时 `--history HEAD` 检查真实历史对象。`--tracked`只覆盖已跟踪文件，不能作为新仓库首次提交的完整检查。扫描器阻断危险路径，即使强制加入本机配置也不会先读取其内容；报错仅给路径和类型，不输出凭据值。扫描有模式覆盖边界，仍须核对实际提交清单。

新克隆需执行 `git config core.hooksPath .githooks` 启用本机hook；远端CI须实际推送后才会运行。只检查配置和文件不会自动暂存、提交或推送。

## 现行文档

| 内容 | 唯一入口 |
|---|---|
| 当前能力、已核运行、未解决事项 | [实施状态](docs/implementation-status.md)、[机器状态](logs/project-current-state.json) |
| 跨层约定与依赖 | [总架构](docs/architecture.txt)、[分层依赖图](docs/implementation-status.md#分层依赖关系) |
| 数据功能与使用 | [数据层清单](docs/data-layer-backlog.md)、[准备与增量操作](docs/first-preparation.md) |
| 金融与策略契约 | [账本](docs/ledger-design.txt)、[执行](docs/execution-design.txt)、[策略](docs/strategy-design.txt)、[模型](docs/model-layer.md) |
| 流程、展示与发布 | [工作流](docs/workflow-design.txt)、[界面](docs/interface-design.txt)、[发布时点](docs/publishing-schedule-design.txt)、[关机就绪](docs/evening-readiness-design.txt) |
| 存储、记录与维护 | [目录](docs/data-layout.md)、[运行观测](docs/runtime-observability-design.txt)、[存储治理](docs/storage-runtime-governance-design.txt) |
| 已发生建设决策及验收入口 | [调整记录](docs/architecture-change-log.md) |
| 配置与部署 | [默认配置](configs/default.toml)、[数据配置](configs/preparation.toml)、[模型配置](configs/models.toml)、[凭据模板](.env.example)、[launchd](deploy/launchd/README.txt)；Web运维说明`deploy/web/README.txt`仅本机留存，不随Git分发 |
