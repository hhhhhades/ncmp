# NCMP 每日评分独立备用调度（Cloudflare Workers）

本目录提供 **独立于 GitHub Actions `schedule` 的备用定时触发器**。GitHub 原生定时任务仍保留：每天北京时间 01:01 开始调度，并随机等待到 01:00–03:00 之间评分。

## 工作机制

- **02:45（北京时间）**：Cloudflare Worker 检查当天是否已有成功评分；若没有，调用 GitHub Actions 的 `workflow_dispatch`。
- **04:15（北京时间）**：如果当天仍没有成功评分，再尝试补执行。
- **去重**：`.github/workflows/auto_score.yml` 使用 job concurrency 串行执行，并在评分前查询当日成功运行记录；原生定时和备用触发同时到达时，正常情况下只评分一次。
- **手动运行**：Actions 页面默认选择 `manual`，仍允许主动重新评分，不受每日去重限制。
- **Cookie 刷新**：每周五北京时间 20:00 的 `Refresh Cookie` 工作流保持原样，不受影响。

> 重要：仓库代码提交并不等于外部调度已启用。必须完成下述 Cloudflare 部署和 Token 配置。GitHub 或 Cloudflare 的故障仍可能导致延迟；这是一套降低漏执行概率的双调度方案，不是绝对准时保证。

## 一、创建专用 GitHub Token

1. 打开 [GitHub Fine-grained Personal Access Tokens](https://github.com/settings/personal-access-tokens)。
2. 新建一个 Token，Repository access 选择 **Only select repositories**，只选 `hhhhhades/ncmp`。
3. Repository permissions 设置 **Actions: Read and write**（Metadata 默认只读）。
4. 设置有效期，记住到期后需要更新 Cloudflare 中的 Token。
5. **不要将 Token 提交到仓库、发送到聊天，或写入 `wrangler.toml`。**

这枚 Token 仅供 Cloudflare Worker 调用 GitHub API，和 Cookie 刷新工作流的 `GH_TOKEN` 分开管理。

## 二、部署 Cloudflare Worker

需要 Cloudflare 账户和本机 Node.js 环境。在仓库根目录打开终端：

```bash
cd backup-scheduler
npx wrangler login
npx wrangler deploy
npx wrangler secret put GITHUB_TOKEN
```

最后一条命令会要求输入上一步创建的 GitHub Token；它作为 **Cloudflare Worker Secret** 保存，不会写进仓库。

如果不方便使用命令行，也可以在 Cloudflare 控制台创建 Worker，使用本目录 `worker.js` 作为脚本，并将 `GITHUB_TOKEN` 配置为 Secret，设置以下 Cron Triggers：

| Cron（UTC） | 北京时间 | 作用 |
| --- | --- | --- |
| `45 18 * * *` | 每天 02:45 | 主备用调度 |
| `15 20 * * *` | 每天 04:15 | 漏执行补偿 |

Cloudflare 的 Cron Triggers 使用 UTC 时区，配置更新可能需要一段时间生效。

## 三、验证

1. 打开 Cloudflare 控制台，进入 `ncmp-score-backup` Worker，确认两个 Cron Triggers 已存在，Secret `GITHUB_TOKEN` 已设置。
2. 查看 Worker 的日志：若当天已成功评分，应出现 `scoring already succeeded`；否则应出现 `backup dispatched Auto Score`。
3. 打开 [GitHub Actions / Auto Score](https://github.com/hhhhhades/ncmp/actions/workflows/auto_score.yml)。备用调度创建的运行类型是 `workflow_dispatch`，输入 `trigger_source=backup`。
4. GitHub 工作流步骤 `Check whether scoring already succeeded today` 会显示当天是否跳过评分。
5. 如日志出现 HTTP 401/403，检查 Token 是否有效及 Actions 权限；如出现 HTTP 404，检查 Token 是否有该仓库访问权限及工作流是否启用。

## 四、运行限制

- GitHub 的原生 `schedule` 可能严重延迟甚至丢失；Cloudflare 调度与其独立，但触发后仍需要 GitHub Actions 有能力接收并执行任务。
- 两次触发只在当日尚无成功评分时补偿；**不能保证**当天一定成功（例如 GitHub API 长时间不可用、Cookie 失效或评分脚本失败）。
- 备用触发的工作流运行可能显示成功但 `Run scoring script` 被跳过，这是去重机制生效，不代表执行了第二次评分。
- 请定期检查 Cloudflare 日志、GitHub Actions 运行记录和 Token 有效期。
