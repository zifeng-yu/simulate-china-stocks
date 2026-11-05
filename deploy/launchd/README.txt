macOS launchd：三个独立任务的现行部署契约

公开Git边界：云端发布是本机独立附加层，唯一实现位于src/china_stock_sim/publishing/。整个模块、configs/publishing*.toml（含模板）、deploy/web/全部材料、上传launcher及专用云端测试不随Git分发；文中这些路径和上传命令只适用于保有附加组件的本机环境。核心通过可选接口接入，缺该层不影响CLI、业务调度、本机首页/交易看板或菜单栏；公开克隆默认提供业务/首页两任务，本机原三任务继续保留。真实配置不改动，部署和运行实际结果只见当前状态与本机收据。

本机菜单栏指示器：除以下原三个按次任务，另有一个独立轻量常驻显示程序，负责30秒只读检查晚间关机就绪，具体方案见docs/evening-readiness-design.txt。该显示程序不采集、不规划、不调用模型，安装/卸载不调整原三任务；现行安装命令shutdown-indicator-build/install/status/uninstall。


当前实际安装、启用、维护及业务结果只在docs/implementation-status.md和logs/project-current-state.json维护。本文说明程序与操作契约，不将历史安装或测试收据当作当前启用证明。完整日序见docs/workflow-design.txt，上传时点见docs/publishing-schedule-design.txt，运行留存见docs/runtime-observability-design.txt，存储维护见docs/storage-runtime-governance-design.txt。

一、本机完整环境的三个任务（公开克隆提供前两项）
1. A股模拟-业务调度：workflow-run --download --scheduled；RunAtLoad、每小时00/15/30/45分及18:10日历触发，负责数据、权益、撮合、日结、策略、报告与备份。
2. A股模拟-首页刷新：dashboard-refresh --scheduled；RunAtLoad及每小时00/15/30/45分的StartCalendarInterval，只读业务状态、原子更新本机首页并执行有界运行日志维护，不等待整轮业务结束。
3. A股模拟-云端上传：dashboard-sync --scheduled；RunAtLoad及每小时00/15/30/45分的StartCalendarInterval补检，仅上传静态展示包，不调用业务workflow。应用仍按上海09:00/21:00及最近已到时点去重，整点已覆盖两个上传时刻，不额外注册重叠日历条目。
三项命令、Label和日志独立，共用本项目Python解释器；未配置KeepAlive，按次执行后退出，注册三项不等于常驻三个Python进程。受控启动器通过exec转入本项目.venv/bin/python -m china_stock_sim --root ABS，保留PID、信号和退出码，不新增一套业务实现。

二、公开维护入口
在项目根目录使用本项目.venv：
  .venv/bin/china-stock-sim workflow-inspect
  .venv/bin/china-stock-sim scheduler-status
  .venv/bin/china-stock-sim scheduler-render
  .venv/bin/china-stock-sim scheduler-install
  .venv/bin/china-stock-sim scheduler-uninstall
首页节点查询为dashboard-status，任务安装状态为dashboard-service-status；任务维护为dashboard-render、dashboard-install、dashboard-uninstall。上传对应dashboard-sync-status、dashboard-sync-render、dashboard-sync-install、dashboard-sync-uninstall。render默认只输出预览，显式--output才保存项目内plist，不安装。status/inspect不执行业务，install/uninstall是明确部署操作；示例文件存在不证明已经安装。
安装前核对项目归属、配置、受控可执行入口和所需启用条件；业务账户未来政策及观察启用必须已明确登记，命令不自动开户、改政策或激活。--scheduled要求对应配置开启，业务market/execution/strategies子开关仍生效。workflow-run即使不带--download，也可能写账、规划、报告和备份。
维护已有任务时先核验原Label与安装字节、确认进程空闲及锁可用，再卸载/更新/重装；活跃财务进程等自然结束，不强杀。用户明确停止业务或批准整组维护时，以该指令和维护回执为准；最终安装、启用、配置匹配与实际启动分别验收，不能凭install成功宣称业务完成。重装的RunAtLoad允许正常触发，不为验收额外kickstart账务。

三、时点、恢复与状态判读
RunAtLoad只在任务被加载时安排一次执行，通常对应真正重启后的用户登录、重新登录或显式安装。三个原任务统一使用每小时00/15/30/45分的StartCalendarInterval，不再配置StartInterval；业务额外18:10，上传09:00/21:00由整点条目覆盖，不叠加重复事件。睡眠跨过至少一个日历时点时，唤醒后合并触发一次，只按当时持久状态检查，不排队回放错过的tick；短睡未跨刻度或仅解锁则不保证立即运行。同Label已有任务继续执行，不强杀、不并行另起；相邻唤醒/登录/日历仍可能先后检查，业务完成记录、锁和上传槽去重保持。next_check_at表示下一个计划日历时点，不再是查询时刻加15分钟；next_retry_at=19:00仍是应用允许恢复点，现行15分钟日历也实际包含19:00。完整契约见docs/workflow-design.txt的CW01—CW08。
业务应用按Asia/Shanghai和真实交易日历判断D/N、18:10日终与N日09:00计划截止；截止后只续办已有冻结单，不补造历史计划。launchd按主机日历触发，TZ环境变量不保证改变其日历时区。关机、睡眠或未登录时不保证执行，恢复按权威账本待办、水位和已保存任务推进，不能以旧RUNNING步骤无限重做。
上传按目标+最近已到的09:00/21:00时点去重，不限交易日。错过多个时点只补恢复时最近时点的最新快照一次，不逐日补历史；00:00—08:59可补前日21:00。同槽成功不重复，09槽成功不吞掉21槽；失败保留成功事实及冷却，跨日不绕过冷却。一个调用固定开始采样时点，不能因传输跨界把旧包登记成下一时点。
dashboard-sync --republish只供明确手工页面维护，与--scheduled互斥，不写入自动plist；允许按最近已到时点同槽重发，仍遵守启用、配置、锁、冷却、时钟和完整性规则。旧schema1日期型成功只读映射该日09:00，不改旧原件；新成功保存last_success_slot。云页按next_due_at判滞后，本机首页按采样超过30分钟判滞后，两者不能混用。
installed/loaded/enabled/file_matches须分别核对。launchctl显示not running可能仅表示任务完成后等待下一触发，不等于卸载。结合last exit code、runtime END、journal总run和公开账本状态判断；最后一个plan COMPLETE或备份预检中的阶段记录不能代表整轮结束。journal是流程审计，金融权威仍为ledger.sqlite3与不可变证据。

四、名称与安装保护（BN01—BN08）
BN01 三个可见名称为“A股模拟-业务调度”“A股模拟-首页刷新”“A股模拟-云端上传”；Label存在不等于系统设置名称已核验。
BN02 本机deploy/launchd/bin中的三个受控薄shell入口仅exec原解释器及原参数，不新增常驻中间进程；公开Git只提供业务/首页两个入口，上传入口随本机独立发布附加层保留。
BN03 名称维护保留Label、工作目录、环境、日志和业务职责；当前触发统一按CW01—CW08使用RunAtLoad及每小时00/15/30/45分日历，业务保留18:10、上传保留09:00/21:00应用门槛。不重签共享Python或清空系统后台项目数据库。
BN04 render/status不创建或chmod入口；install验证路径、普通可执行文件、精确模板及解释器，未知安装归属拒绝覆盖。
BN05 入口变更按空闲、归属、原配置和锁核验后逐项维护；不得为改名中断账务。
BN06 入口原字节/执行权限纳入身份，覆盖中文空格路径、参数/退出/PID/信号及替换入口拒绝。
BN07 配置/实际调用与系统设置名称分别记录，业务在途明确RUNNING；BTM诊断不可用时如实说明，不为诊断索取密码、清库或修改其他应用。
BN08 仅维护现行入口，历史名称和旧验收作为发生时证据保留；命令1/2、账户身份和既有数据不变，无需重下。

五、运行记录与留存
workflow/homepage/upload逐次保存logs/runtime/YYYY-MM-DD/<invocation_id>.jsonl，START/EVENT/END包含有界身份、资源和业务引用；原stdout/stderr与journal是补充。禁用、锁忙、冷却和同槽空操作也有记录；缺END结合PID出生身份区分在途、中断或未知，不能猜测launchd唤醒原因。
运行明细默认30天、压缩归档180天，按configs/runtime_retention.toml及完整保护清单维护；活进程、身份未知、未解决故障和金融/封口引用优先保护。stdout/stderr仅停机或已确认空闲时按10MiB×5软轮换，不copytruncate活跃fd。首页既有任务在有界archive_due之后每日最多维护一次；清单不完整则WAITING且不删除，留存维护不另增任务。
runtime-history及runtime-archive-verify只读；runtime-archive是明确写入data/backups/runtime的归档操作。归档单日期/字节预算、内容指纹、失败checkpoint续办与保护规则见运行附件；同盘运行日志归档不等于全部金融/市场备份或异机容灾。已封口原字节通过common.audit_archive解析历史SHA，不能把压缩wrapper当旧原件。

六、云端边界
云端仅运行Ubuntu上的静态Web服务；真实主机、SSH用户/端口、代理与私钥位置由本机未提交的configs/publishing.toml和SSH配置提供。公开示例使用example.invalid:8080，不代表实际部署。Nginx模板、实际配置和Web说明全部仅本机留存，整个deploy/web/不随Git分发；有附加材料的维护者按实际环境部署，仅服务两页白名单包，业务Python、数据库、模型、凭据与原始审计留本机。真实访问地址及发生时验收仅在未提交的logs/project-current-state.json和对应本机收据中查看，完整本机运维契约见deploy/web/README.txt，该文件不由公开Git提供。

七、历史证据索引（不代表当前安装状态）
• 2026-11-01隔离launchd探针：logs/workflow-implementation-2026-11-01-launchd-verification.json；覆盖安装/实际触发/中断恢复，不等于物理睡眠或重启实测。
• 2026-11-02两页云部署与视口浏览器范围：logs/cloud-deploy-2026-11-02-http-verification.json、logs/cloud-deploy-2026-11-02-browser-acceptance.json；旧服务器地址、单09时点和next_due只属于当时。
• 2026-11-02运行观测：logs/runtime-observability-2026-11-02-validation.json、logs/runtime-observability-2026-11-02-runtime-live-sample.json；在途采样不等于整轮完成。
• 2026-11-02双时点发布：logs/publishing-twice-daily-2026-11-02-validation.json、logs/publishing-twice-daily-2026-11-02-runtime-verification.json。
• 2026-11-02名称与实际设置：logs/background-task-names-2026-11-02-runtime-verification.json。
• 2026-11-03业务停止历史：logs/business-scheduler-stop-2026-11-03.json；后续存储维护、规则内恢复及最终部署分别有新回执，不把停用历史当永久状态。
• 2026-11-03源代码静态与专项闭环：logs/storage-governance-2026-11-03-code-closeout.json；真实报告、云端和三个任务的最终部署证据由当前入口统一索引。

八、业务自动避让（现行CR16）
自动业务在每日17:00（含）—19:00（不含）跳过触发，19:00后首次检查或开机RunAtLoad继续到期工作；18:10仍是当日数据可用与账务日期门槛，不因避让改成新交易日。跳过只记录运行观察，退出0，不更新业务完成、重试、账本或备份；首页刷新和09:00/21:00上传不受影响。原三个Label、每小时00/15/30/45分检查、18:10日历触发、RunAtLoad和名称保持；门禁在业务锁与任何市场/账本/模型工作之前。用户约19:10开机时正常RunAtLoad按最新持久待办续做，不排队重跑每个错过tick。原子事务不在17:00强杀；只读inspect与首页可显示避让状态，人工明确维护命令保留。最终重装启用及真实运行证据查当前入口。
