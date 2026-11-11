# 架构与建设决策索引

本文件仅保留已发生的关键决策、边界和证据入口，现行规则以[总架构](architecture.txt)及各层唯一附件为准，当前能力/运行以[实施状态](implementation-status.md)为准。下表中的失败、待验收、旧服务器、旧schema和旧计数均属于对应日期，不作为当前方案或当前数据。

完整过程保存在日期化回执；原长篇说明已按[本轮整理基线](../logs/directory-consolidation-2026-11-05-baseline.json)封存原字节。需要追溯未单列回执的早期说明时，使用audit_archive按原SHA解析，不恢复成另一套运行文件。原文SHA：`a6451f91df4a6e4ef27fcc9597cadc5fd12a4e803082a0f77fa6c06984262368`。

## 近期决策

| 日期/事项 | 决策与边界 | 证据 |
|---|---|---|
| 2026-11-11 CI运行镜像固定 | `ubuntu-latest`切换Ubuntu 26.04后无法安装锁定的Python 3.12.10；固定Ubuntu 24.04，Python/依赖锁/安全扫描/全量测试保持。生产运行不变，最终CI结论按实际提交回执记录。 | [公开Git审查与CI回执](../logs/git-push-2026-11-11-receipt.json) |
| 2026-11-11 持仓买入字段 | 汇总新增原买入日期/价格，多批次编号对应；仅展示维护，86项验证通过。本机重建及云端重发完成，线上三账户字段HTTP核验通过；浏览器视觉检查因工具超时未完成。 | [本轮验证](../logs/holding-buy-fields-2026-11-11-validation.json) |
| 2026-11-10 WP三项耗时优化与全项目整理 | 一次证据序列化/摘要回执/静态冻结投影，同次数据更新原件核验复用，同轮账单输入复核与窄状态；动态指纹、精确cutoff、CAS与冻结完整性保持。三户11月11日同参数维护，原任务恢复；全项目按用途清理缓存、原位归纳文档，金融/审计/有效恢复证据保留。 | [唯一方案](strategy-performance-design.txt)、[3641项验证](../logs/workflow-performance-2026-11-10-test-summary.json)、[性能报告](../logs/workflow-performance-2026-11-10-performance-report.html)、[最终矩阵](../logs/workflow-performance-2026-11-10-final-acceptance.json) |
| 2026-11-09 SP规划与冻结恢复性能 | 同次输入复用、目标日前可信初始化查询、显式摘要冻结/恢复及细分观测；算法/参数/模型不变，三户11月10日未来维护政策采用，旧事实保留。冷核与热路分别计量；不包含备份、WAITING_INPUT与无请求数据检查优化。 | [方案](strategy-performance-design.txt)、[必要验证](../logs/strategy-performance-2026-11-09-test-summary.json)、[性能报告](../logs/strategy-performance-2026-11-09-performance-report.html)；[实际恢复](../logs/strategy-performance-2026-11-09-natural-run.json)与[最终收口](../logs/strategy-performance-2026-11-09-final-acceptance.json)已完成；首轮冷核成本单列 |
| 2026-11-05 CI平台契约修复 | Ubuntu全量检查的唯一失败为测试硬编码macOS返回值；显式检验Darwin/Linux状态与退出码、只读且无构建安装，生产逻辑不改；全量CI保留并输出最慢20项耗时。 | [核验方案](../logs/github-ci-repair-2026-11-05-plan-review.json)、[本机12项验证](../logs/github-ci-repair-2026-11-05-test-validation.json)；远端结论见对应提交Actions |
| 2026-11-05 LP独立发布与公开Git边界 | 上传保留唯一独立本机层，完整程序/配置/模板/专用部署与测试不提交；核心可选接入，公开副本可用，本机原三任务继续，安装定义匹配无需重装。 | [验收](../logs/local-publishing-boundary-2026-11-05-final-acceptance.json) |
| 2026-11-05 GP首次公开仓库准备 | 脱敏、凭据/运行数据排除、真实候选扫描及空索引保护；先前允许提交发布模板的范围已由LP收紧，历史结果保留。 | [发生时验收](../logs/git-push-preflight-2026-11-05-final-acceptance.json) |
| 2026-11-05 DO目录归纳与清理 | 全目录核对后可撤销移除退役外层材料及无业务缓存，主项目文档归纳；金融事实、引用证据及层次保持，不扩展程序优化。最终完成范围见当前入口。 | [方案核验](../logs/directory-consolidation-2026-11-05-plan-review.json)、[实际清理](../logs/directory-consolidation-2026-11-05-cleanup-result.json) |
| 2026-11-05 CW日历触发 | 三任务改每小时四刻日历、登录加载；睡眠漏刻度合并一次当前检查，运行中不并发/不补tick。148项必要测试、重装与实际首轮退出0；无物理睡眠实测。 | [验收](../logs/calendar-wake-2026-11-05-final-acceptance.json)、[实际运行](../logs/calendar-wake-2026-11-05-runtime-verification.json) |

## 历次建设索引

以下按发生日期倒序排列；保留原事项编号以便对照历史引用。验收入口优先选封口总结/矩阵，不在此再次复制测试输出、图片经过或安装日志。

| 日期 / 原事项 | 发生时决策与准确边界 | 证据入口 |
|---|---|---|
| 2026-11-04 · 本机菜单栏关机就绪指示器 | ER01—ER12分层实现只读D/N判定及原生菜单栏；补充可靠锁owner边界。156项测试和用户菜单确认，不冒称物理睡眠实测。 | [回执1](../logs/evening-readiness-2026-11-04-final-acceptance.json) |
| 2026-11-04 · 交易看板重点信息、中文交互与只读损益投影 | TU优先展示计划、成交、行情和损益，中文状态与双图，有界只读投影；原机器码和私有审计保留。 | [回执1](../logs/trading-dashboard-2026-11-04-final-acceptance.json)、[回执2](../logs/trading-dashboard-2026-11-04-test-summary.json) |
| 2026-11-04 · TH：历史个股损益、买入成本批次与完整成交展示 | TH日期联动损益、含费均价/剩余批次及全部成交/清仓汇总、分页CSV完成；不新增行情采集，保留缺证边界。 | [回执1](../logs/trading-history-2026-11-04-final-acceptance.json) |
| 2026-11-03 · 连续日结、证据复用与自动业务避让 | CR01—CR16修正普通预案阻断、同义日结、覆盖证明及备份重绑；原金融身份保留，增加17—19业务避让。 | [回执1](../logs/continuity-repair-2026-11-03-final-acceptance.json) |
| 2026-11-03 · 晚间实际业务与性能复核 | 五轮完成11月3日业务及次日计划，后轮去重通过；发现冻结恢复大原件与等待输入复用两个性能跟进，未宣布全部解决。 | [回执1](../logs/evening-acceptance-2026-11-03-final-acceptance.json) |
| 2026-11-02 · 运行首页、独立静态发布与规则策略每日限制调整（程序及自动化验收完成） | RH首页、独立静态发布和NL限制调整完成；仅LLM最多5代码，原账户及11月2日冻结计划保留，未来政策衔接。 | [回执1](../logs/run-home-2026-11-02-final-acceptance.json)、[回执2](../logs/run-home-2026-11-02-validation.json) |
| 2026-11-02 · Ubuntu静态站点实际部署与首次上传 | 原Ubuntu站点完成最小Nginx部署、首次上传和远程浏览器验收；旧服务器及09单槽仅属当时。 | [回执1](../logs/cloud-deploy-2026-11-02-launchd-verification.json)、[回执2](../logs/cloud-deploy-2026-11-02-http-verification.json) |
| 2026-11-02 · 交易页统一首页主题与后台任务复核 | 交易页统一首页主题，纯幂等换肤保留金融数据；核验三任务职责独立，未误删必要任务。 | [回执1](../logs/dashboard-style-2026-11-02-http-verification.json)、[回执2](../logs/dashboard-style-2026-11-02-ui-validation.json) |
| 2026-11-02 · 三任务运行观测、性能与长期留存契约 | RO逐调用START/阶段/END、CPU/RSS、身份证据、异常与归档建设；此前未记录性能不能补造。 | [回执1](../logs/runtime-observability-2026-11-02-validation.json)、[回执2](../logs/runtime-observability-2026-11-02-plan-review.json) |
| 2026-11-02 · 16:22：三任务运行复核与在途业务结束确认 | 16:22只读核对三任务，确认15:41业务已16:12正常结束；该次30分24秒只属于当时。 | [原说明封存基线](../logs/directory-consolidation-2026-11-05-baseline.json) |
| 2026-11-02 · 发布增加21:00，错过多个时点只补最新一次 | PS09/21双槽与最近已到槽去重完成；多次错过只传最新一次，凌晨可补前日21槽。 | [回执1](../logs/publishing-twice-daily-2026-11-02-runtime-verification.json)、[回执2](../logs/publishing-twice-daily-2026-11-02-validation.json) |
| 2026-11-02 · 后台任务显示名称 | BN原三Label改为受控命名shell入口exec原Python，保持PID/参数/日志；显示名称和安装分别核验。 | [回执1](../logs/background-task-names-2026-11-02-final-acceptance.json) |
| 2026-11-01 · 正式启用与首页摘要维护（手工与自动运行均完成） | 原S1/S1-LT/LLM明确追加未来政策、激活并启用调度；原base、注资和账户身份保留，真实手工/自动运行另留回执。 | [回执1](../logs/production-start-2026-11-01-scheduler-verification.json)、[回执2](../logs/production-start-2026-11-01-display-validation.json) |
| 2026-11-01 · 全项目完成状态复审（迟到输入恢复修复及全量验收通过） | 按实际代码修正文档状态；补充迟到业务事实的复核、范围重述和恢复，避免FINAL永久挡住必要更正。 | [回执1](../logs/project-completion-audit-2026-11-01-final-acceptance.json)、[回执2](../logs/project-completion-audit-2026-11-01-doc-review.json) |
| 2026-11-01 · 完整调度与流程实现（WF01—WF32完整验收通过） | WF01—WF32整日编排、持久恢复、日历、共享锁、预算、报告及日备份完成；只读检查与业务写命令分开。 | [回执1](../logs/workflow-implementation-2026-11-01-final-acceptance.json) |
| 2026-11-01 · 三策略完整实现与独立规划/发布（完整验收通过） | ST/AC实现三策略独立入口、确定性评分数量、五日父任务、模型持久占位/响应恢复和严格验证。 | [回执1](../logs/strategy-implementation-2026-11-01-requirements.json)、[回执2](../logs/strategy-implementation-2026-11-01-pytest.txt) |
| 2026-11-01 · 当前入口与封口证据统一 | 人读与机器当前入口统一；封口验收保存原发生时内容和摘要，不要求永远等于后续当前文件。 | [回执1](../logs/project-current-audit-2026-11-01-final-acceptance.json)、[回执2](../logs/project-current-state.json) |
| 2026-11-01 · 分类修正、离线重建与1771项回归（独立保护终验PASS） | SC01—SC04修正会计合并报表/预案阶段误分类，既有原件离线重建；保留真实复杂事项和来源冲突。 | [回执1](../logs/event-classification-repair-2026-11-01-requirements.json)、[回执2](../logs/event-classification-repair-2026-11-01-verification.json) |
| 2026-11-01 · 用途等待与来源冲突只读复核 | 只读用途检查区分来源缺项、真实冲突与两个标准化程序缺陷；当时计数不是当前覆盖。 | [回执1](../logs/purpose-readiness-2026-10-30-review.json)、[回执2](../logs/purpose-readiness-2026-10-30-settlement-review.json) |
| 2026-11-01 · 月末第三步新池补缺完成 | 显式以10月30日补新池代码，复用94股合格输入，对6股追加必要权益请求；保留按用途等待。 | [回执1](../logs/month-end-new-pool-2026-10-30-final-acceptance.json)、[回执2](../logs/month-end-new-pool-2026-10-30-verification.json) |
| 2026-11-01 · 10月末命令1准备通过与退出错误最小修复 | 月末命令1完成准备；如实保留首轮内外状态不一致和退出异常，修正后按冷却续办，不回写失败记录。 | [回执1](../logs/month-end-preparation-2026-10-30-verification.json)、[回执2](../logs/month-end-preparation-2026-10-30-worker-fix-verification.json) |
| 2026-11-01 · 10月30日命令2完成与重试/HTTP最小修复 | 命令2分轮补齐10月30日，连续失败退避与HTTP预算修复；前轮PARTIAL保留，末轮完成另留证。 | [回执1](../logs/data-sync-2026-10-30-final-acceptance.json)、[回执2](../logs/data-sync-2026-10-30-verification.json) |
| 2026-11-01 · SL01—SL04完整验收与生产schema3迁移 | SL01—SL04开户、政策/决策、模型绑定和备份恢复完成；当时源schema2→3迁移不代表现行schema4。 | [回执1](../logs/ledger-strategy-support-2026-11-01-requirements.json)、[回执2](../logs/ledger-strategy-support-2026-11-01-verification.json) |
| 2026-11-01 · 当前项目整理与会话续接 | 统一项目状态、阅读顺序与分层入口，仅核对文字/代码/验收，不重新载入会话图片或并行实现。 | [回执1](../logs/project-state-review-2026-11-01.json) |
| 2026-10-31 · INTERFACE-20261030-24 · 人工浏览器验收与JSON兼容性修正 | 10月31日完成M01—M06、V01—V06用户浏览器验收；长ID/中文审计/JSON兼容修正与复验保留各自时点。 | [回执1](../logs/interface-si01-2026-10-30-browser-acceptance.json)、[回执2](../logs/interface-si01-2026-10-30-readable-audit.json) |
| 2026-10-30 · INTERFACE-20261030-23 · 纠正浏览器验收状态并记录实际阻塞 | 记录file协议受限时无法直接视觉验收，撤回超出实际证据的“全部完成”；后续验收另留发生时记录。 | [回执1](../logs/interface-si01-2026-10-30-browser-acceptance.json)、[回执2](../logs/interface-si01-2026-10-30-browser-acceptance.json) |
| 2026-10-30 · INTERFACE-20261030-22 · 配置、入口与看板 SI01 | SI01动态账户/本金、政策身份、看板与CLI接入；不能用源码和自动测试冒称浏览器实测。 | [回执1](../logs/interface-si01-2026-10-30-verification.json) |
| 2026-10-30 · EXEC-20261030-21 · 策略决策证据贯通 SX01 | SX01贯通策略决策引用、冻结原件和执行验证，未知/损坏证据拒绝金融变更。 | [回执1](../logs/execution-sx01-2026-10-30-verification.json) |
| 2026-10-30 · MODEL-20261030-20 · 模型脱敏与安全审计支持 SM01 | SM01完善模型脱敏、安全快照和审计接口，连接层不承担策略判断。 | [回执1](../logs/model-sm01-2026-10-30-verification.json) |
| 2026-10-30 · DATA-20261030-19 · 策略自定义窗口公开契约 SD01 | SD01开放有界、自定义历史窗口与knowledge-time契约供策略读取，不预先执行策略。 | [回执1](../logs/data-sd01-2026-10-30-verification.json) |
| 2026-10-30 · DOC-20261030-18 · 最新状态补核与迁移范围澄清 | 核对数据/账本版本、准备任务与当前入口，澄清设计状态和实际迁移范围。 | [回执1](../logs/strategy-design-2026-10-30-verification.json) |
| 2026-10-30 · STRATEGY-20261030-17 · 策略完整方案、可新增账户及可审计决策 | 确立三策略、可新增账户与可审计决策完整方案；先列清上下游依赖，不把方案当实现。 | [回执1](../logs/strategy-design-2026-10-30-requirements.json)、[回执2](../logs/strategy-design-2026-10-30-verification.json) |
| 2026-10-30 · DOC-20261030-16 · 现行文件、程序状态与分层依赖复核 | 交叉核对现行文件、代码与验收，明确各层责任和依赖方向。 | [回执1](../logs/project-state-review-2026-10-30.json)、[回执2](../logs/project-state-review-2026-10-30-tests.txt) |
| 2026-10-30 · EXEC-20261030-15 · 规则与模拟执行层完整实施验收 | 完整执行层验证、开盘参考撮合、冻结/结算恢复及故障边界实现验收；计算纯函数与账本写分离。 | [回执1](../logs/execution-design-2026-10-29-requirements.json)、[回执2](../logs/execution-build-2026-10-30-verification.json) |
| 2026-10-30 · SI01 组件方案确认（实现前） | 实现前确认SI01-C1—C8，种子配置与持久账户登记分工、历史身份及缺证展示边界。 | [原说明封存基线](../logs/directory-consolidation-2026-11-05-baseline.json) |
| 2026-10-29 · EXEC-20261029-14 · 执行层方案与上下游依赖复审 | 执行方案复核数据/账本依赖、日期、费用和恢复边界后进入建设。 | [回执1](../logs/execution-design-2026-10-29-requirements.json)、[回执2](../logs/execution-review-2026-10-29-verification.json) |
| 2026-10-29 · DOC-20261029-13 · 数据状态文档全面同步 | 按真实采集和用途结果同步数据文档，避免任务COMPLETE被误解为全部用途READY。 | [回执1](../logs/data-sync-2026-10-29-verification.json)、[回执2](../logs/data-state-doc-review-2026-10-29-verification.json) |
| 2026-10-29 · RUN-20261029-12 · 主源恢复后命令2补齐今日数据 | 主源恢复后按已存进度和冷却补齐10月29日，新增采集与此前缓存复用分别记录。 | [回执1](../logs/data-sync-2026-10-29-final-acceptance.json)、[回执2](../logs/data-sync-2026-10-29-verification.json) |
| 2026-10-29 · RUN-20261029-11 · 10月29日晚间增量与运行问题修正 | 10月29日晚间增量记录来源错误、修正及续跑；保留每轮实际结果而非改写先前状态。 | [回执1](../logs/data-sync-2026-10-29-final-acceptance.json)、[回执2](../logs/data-sync-2026-10-29-verification.json) |
| 2026-10-29 · SUPPORT-20261029-10 · 数据3项与账本5项执行支持全部完成 | 补齐执行消费者所需数据3项及账本5项公开支持，并逐项验证依赖。 | [回执1](../logs/execution-support-2026-10-29-requirements.json)、[回执2](../logs/execution-support-2026-10-29-verification.json) |
| 2026-10-29 · EXEC-20261029-09 · 规则与模拟执行层完整方案及依赖核查 | 完整执行方案及依赖清单建立，明确需要先补齐的公开事实和金融支持。 | [回执1](../logs/execution-design-2026-10-29-requirements.json)、[回执2](../logs/execution-design-2026-10-29-verification.json) |
| 2026-10-29 · DOC-20261029-08 · 账本完成后状态复核与账户范围补记 | 账本建设后复核账户初始化、余额及当前边界，不把初始账户当已开始交易。 | [回执1](../logs/project-state-review-2026-10-29-post-ledger.json) |
| 2026-10-29 · LEDGER-20261029-07 · 账本完整建设（开发、自测与验收完成） | 账本核心资金/持仓/权益/日结、事务幂等、更正、备份与公共接口完成建设验收。 | [回执1](../logs/ledger-build-2026-10-29-requirements.json)、[回执2](../logs/ledger-build-2026-10-29-verification.json) |
| 2026-10-29 · DOC-20261029-06 · 最新项目状态、数据与账本记录一致性复核 | 按真实数据、账本和记录对齐状态说明，不借旧文件描述现行缺项。 | [回执1](../logs/project-state-review-2026-10-29.json) |
| 2026-10-29 · DATA-20261029-05 · 数据D15—D17一次性开发、验收及账本依赖复核 | D15—D17权益动作、历史未完结权益和税基来源/UNKNOWN契约实现并完成消费者核查。 | [回执1](../logs/data-ledger-extension-2026-10-29-requirements.json)、[回执2](../logs/data-ledger-extension-2026-10-29-verification.json) |
| 2026-10-29 · LEDGER-20261029-04 · 账本完整设计、依赖核查与方案自检（历史设计记录） | 先明确账本范围及数据依赖、核验方案；本条只记录设计时点，后续实现见单独回执。 | [回执1](../logs/ledger-design-2026-10-29-verification.json) |
| 2026-10-29 · DOC-20261029-03 · 明确日常增量与月末换池日程 | 集中确立每交易日命令2、真实月末命令1及新池后继生效；自然月末不等于最后交易日。 | [原说明封存基线](../logs/directory-consolidation-2026-11-05-baseline.json) |
| 2026-10-29 · RUN-20261029-02 · 命令2同步、用途隔离与正式运行约束 | 命令2与用途隔离通过；要求正式运行由程序自主执行，不依赖聊天助手指挥。 | [回执1](../logs/data-sync-2026-10-28-verification.json)、[回执2](../logs/data-sync-2026-10-28-cache-20261029T130544.result.json) |
| 2026-10-29 · RUN-20261029-01 · 首次100股历史准备及运行中修正 | 首次固定100股、121交易日历史准备及权益线索采集完成；原日期范围与运行修正保留。 | [回执1](../logs/first-preparation-2026-10-28-verification.json) |
| 2026-10-28 · DATA-20261028-02 · 独立数据层实现与验收 | 独立数据服务及错误/冷却/修订/知识时点等验收完成，替代先前“仅设计”状态。 | [原说明封存基线](../logs/directory-consolidation-2026-11-05-baseline.json) |
| 2026-10-28 · DATA-20261028-01 · 数据层独立性复查（历史审计，当时未实施） | 最初只读审计数据层独立性、缺项和修复方案；当时未实施或采集，后续完成另记。 | [原说明封存基线](../logs/directory-consolidation-2026-11-05-baseline.json) |
| 2026-10-28 · 代码审计记录 | 数据代码审计位置、缺项与验收条件收敛到数据层清单。 | [原说明封存基线](../logs/directory-consolidation-2026-11-05-baseline.json) |
| 2026-10-28 · 本轮文件范围 | 新增数据层清单和调整记录并原位同步架构/状态/目录，不另建运行工程。 | [原说明封存基线](../logs/directory-consolidation-2026-11-05-baseline.json) |
