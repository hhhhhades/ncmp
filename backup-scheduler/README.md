# NCMP 独立备用调度

本目录是可部署的 Cloudflare Worker。代码提交、离线测试和编译检查均不等于已上线；需要真实账户部署、设置 Secret，并核验线上运行。

## 时间与原配置

| 来源 | 北京时间 | 行为 |
| --- | --- | --- |
| GitHub 原生 schedule | 01:01 启动 | 随机等待到 01:00–03:00 内评分；严重延迟时立即补执行 |
| Cloudflare Cron `45 18 * * *` | 每天 02:45 | 无当天完成记录时派发备用运行 |
| Cloudflare Cron `15 20 * * *` | 每天 04:15 | 再次检查并补偿漏执行 |
| Refresh Cookie | 每周五 20:00 | 原工作流保持原样 |

保留 QQ 邮箱 SMTP `smtp.qq.com:465`（及原有通知后备方式）、`SCORE`、`WAIT_TIME_MIN/MAX`、`FULL_EXTRA_TASKS` 等 Secrets。

## 完成记录与去重

- 所有原生、备用、手动运行都使用固定 concurrency group `ncmp-daily-score`，仅 main 分支允许评分。手动运行默认也去重，没有绕过去重的选项。
- 工作流在等待前、评分前各查一次完成记录；查询失败直接停止评分。Worker 的检查只是减少无用派发，查询失败可以派发，不能绕过工作流检查。
- 只认可精确名 `ncmp-score-completed-v1-YYYY-MM-DD` 的未过期 artifact，核验创建北京日期、main 分支及所属 `auto_score.yml` 运行。查询支持分页。不用 workflow 的 success 或 run_started_at 推断是否评分。
- `main.py` 只在网易云服务端确认每日和配置要求的额外任务完成后，返回 0 并生成无凭据的 JSON 回执。工作流再次校验回执后才上传完成记录。
- 因去重或验证模式而跳过的成功 workflow 不写完成记录。有效的服务端“任务已完成”状态可以生成回执，其中 submitted=0；这与工作流跳过不同。
- 网络超时不自动重发结果不确定的评分 POST；失败后下一次运行先读取服务端已完成状态，只处理未完成作品。405 资源跳过、部分未完成不能冒充成功。
- 备用请求携带明确的北京日期；过期或未来请求不会评分。Cloudflare 使用 scheduledTime，延迟跨日回调直接跳过。评分脚本在提交前后检查日期，不能跨日继续提交。
- 日内完成记录至少保留 30 天。不要手动删除当天 artifact。中途取消、完成记录上传失败、人工删除记录或外部操作都可能破坏全局去重；服务端完成状态是重试时的第二道保护。客户端无法对远端评分 API 提供事务式 exactly-once 保证。

## GitHub Token：仅限单仓库 Actions 权限

在 [Fine-grained tokens](https://github.com/settings/personal-access-tokens/new) 创建专用 Token：

1. Resource owner：`hhhhhades`。
2. Repository access：Only select repositories，只选 `ncmp`。
3. Repository permissions：**Actions: Read and write**；Metadata 保持默认只读，其他权限不勾选。
4. 设置合适有效期并记下更新日期。过期前更新 Worker Secret。

不要把 Token 发到聊天、提交代码、放入命令参数、URL、日志或普通变量配置。Cookie 刷新的现有 `GH_TOKEN` 是另一枚凭据，无需修改。Token 使用范围依据 [workflow_dispatch 权限文档](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)。

## 部署

在本目录打开终端，使用专用 Cloudflare 账户完成授权：

```powershell
npx --yes wrangler@4.162.0 login --use-keyring --scopes account:read user:read workers_scripts:write workers_tail:read
npx --yes wrangler@4.162.0 deploy
npx --yes wrangler@4.162.0 secret put GITHUB_TOKEN
npx --yes wrangler@4.162.0 secret list
npx --yes wrangler@4.162.0 deployments list
```

仅在 `secret put` 的交互提示中输入 Token，或在 Cloudflare 控制台选择 **Secret / Encrypt**，名称设为 `GITHUB_TOKEN`。不要使用 `--var GITHUB_TOKEN=...`。Secret 不可从控制台重新读取；核验只查看名称及类型。部署前若同名 Worker 已存在，先核对账户、当前代码和版本，避免覆盖无关服务。

`wrangler.toml` 已包含两个 UTC Cron 和 observability。Cloudflare 控制台中核对 `ncmp-score-backup` 的线上版本、上述两个 Cron、Secret 名称及日志状态。Cron 使用 UTC，配置传播最多可能需要 15 分钟，依据 [Cloudflare 文档](https://developers.cloudflare.com/workers/configuration/cron-triggers/)。由 Wrangler 管理时，后续修改 Cron 应同步此配置文件。

## 线上验证（避免为了测试重复评分）

Worker 的其他 HTTP 路由返回 404，不提供公开评分接口。`POST /verify` 需要请求头 `Authorization: Bearer` 携带同一专用 Token，拒绝查询参数中的凭据；它仅派发 `verification_only=true`，不会真实评分或生成完成记录。不要将包含认证头的请求打印或录制到日志。

1. 核对 Cloudflare deployment ID、Cron 配置和 Secret 名称；检查 Worker 日志已开启。
2. 使用受保护的 `/verify` 发起一次验证，记录响应中的 request_id。
3. 到 [Auto Score](https://github.com/hhhhhades/ncmp/actions/workflows/auto_score.yml) 找到标题中相同 request_id 的 `workflow_dispatch`。
4. 检查 guard 日志和 summary 显示 `verification only; would_run=...`，评分步骤和 artifact 上传步骤均跳过。这个成功运行不能计为已评分。
5. 在下一次真实 Cron 时间，检查 Worker 日志 `already_scored` 或 `dispatched`。若派发，核对 request_id、实际评分步骤和当天完成回执；不要只看绿色工作流状态。
6. 实际评分后，追加一次普通手动运行应由 guard 跳过，且不能额外产生完成记录。

结构化 Worker 日志包含 date、request_id、status。常见状态：`already_scored`、`dispatched`、`verification_dispatched`、`expired_cron`、`marker_check_failed`、`dispatch_failed`。401/403 检查 Token 有效期和 Actions 权限；404 检查单仓库授权、工作流文件和启用状态。错误日志不输出 GitHub 响应正文或 Token。

离线验证：

```powershell
node --test backup-scheduler/worker.test.mjs
python -m unittest tests.test_daily_guard tests.test_scoring_result
```

以上从仓库根目录运行。GitHub 的 `Backup Scheduler Checks` 会在相关变更提交后运行同样的无网络回归测试；该工作流不读取评分 Secrets，不执行真实评分。

GitHub/Cloudflare 故障、Cookie 失效和 Token 过期仍可能导致漏执行。上线验收必须区分：代码检查通过、已部署配置、验证派发通过、真实定时评分通过。没有对应证据时，不应报告后续阶段已完成。
