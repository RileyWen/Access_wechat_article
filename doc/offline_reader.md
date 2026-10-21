# 重建阅读页与分阶段补采

主服务新增默认勾选的“重建阅读页（含本地图片）”。它从保存的正文生成干净的本地 HTML，在文章底部显示留言和回复，与原来的 Playwright“离线缓存”分开。

勾选“评论信息”后，采集会下载一级留言及回复的头像。评论原始 URL 保留在 `comments/final.json`，头像保存在 `comments/assets/avatars/`，关联和下载结果记录在 `comments/assets/manifest.json`。

重建页保存在文章目录的 `reader/index.html`，正文图片保存在 `reader/assets/img/`。页面不执行原站脚本、不加载远程头像或图片；可以双击打开，也可以移动整个文章目录后阅读。图片缺失显示本地占位及数量提示。音视频不属于重建页这一版的下载范围，原离线缓存的媒体功能继续使用原来的入口。

## 查看完整性

“数据档案 → 记录详情”显示六个阶段：文章详情、评论及回复、评论头像、正文图片、重建阅读页、原离线缓存。每个阶段都有当前产物状态、数量、最近尝试结果和错误提示。

- “完整”需要有效文件和完整性证据，单纯存在目录或 JSON 不代表完整。
- 评论达到分页上限、重复分页但未正常结束、回复数量不足，都属于不完整。
- 未能解析评论数不会当作零评论，也不会删除已有评论。
- “当前完整；本次失败”表示保留了上次可读数据，本次更新没有成功。
- 网页缓存按实际页面引用、文件有效性及加载是否结束核验；监控接口等非关键下载警告单独记录，不直接判为不完整。
- 初次获取详情就失败的文章保留在“未形成文章索引的待补采目标”中。

文章列表显示评论、头像、正文图片和阅读页的简要状态。“资源明细”可查看全部阶段及具体原因，网页缓存是当前保存的网页副本，与代码版本无关。数据库增加任务目标和阶段状态表，原有三张表保持兼容。首次迁移通过 SQLite backup API 生成同目录的 `*.before-reader-v1.bak`。程序重启会把运行中断的阶段改成可重新排队的待处理状态。

## 补采与更新

1. 在记录详情勾选文章，选择要处理的阶段。
2. 点击“补齐选中”只处理缺失、失败或待核验阶段；“补采本篇”处理单篇。
3. “强制更新所选阶段”重新获取这些阶段的数据。完整评论更新会重新下载所有头像，成功后整体替换旧评论目录，不累计旧版本。
4. “仅待补齐／仅完整”按照当前选择的阶段筛选整个公众号的记录。
5. 点击“阅读页”打开本地重建页；“停止补采”复用主服务的停止操作。

能用已有 URL 补下载的头像、正文图片及本地页面重建，不需要打开微信。评论缺页、正文缺失或凭据过期则需要微信：先打开对应公众号主页并回到顶部，程序沿用主页扫描、点击和 MITM 捕获流程取得新凭据，只打开选定目标。

一次补采仅处理一个公众号。标题匹配不唯一、打开后的稳定文章链接不符、扫描结束未找到目标，都会明确记录失败，不会用其他文章替代。正常采集的“跳过已完整记录”仅跳过本次所选资源均完整的文章。

下载、校验在暂存区执行，成功后统一提交文件和数据库。更新失败或取消时保留旧的可读产物；部分首次采集结果会标为不完整。局部补图会复用有效文件，只重新下载缺失项；上次强制更新失败的阶段会重新尝试下载。

## 外部代理

资源下载不使用临时 MITM 代理，也不改变系统代理。代理按以下顺序读取：

1. 进程环境变量 `AWA_RESOURCE_PROXY`。
2. `data/custom.yaml` 的 `resource_download.proxy_url`。
3. 被 Git 忽略的 `data/runtime/resource-download.json` 中的 `proxy_url`。
4. 当前外部系统代理；忽略本地临时代理。

对于会在采集时切换系统代理的机器，应配置上述前三种之一。当前 Windows 执行机使用已约定的局域网外部代理，写入被忽略的运行配置，避免采集切换代理时影响图片下载。

## 接口

- `GET /api/archive/accounts/{id}/articles`：新增 `resources` 和 `readerAvailable`；支持 `resourceStatus=all|complete|incomplete` 与逗号分隔的 `resourceStages`。
- `GET /api/archive/pending-targets`：查看尚未形成文章索引的目标。
- `POST /api/archive/articles/retry`：`articleIds` 或 `targetKeys`、阶段数组 `resources`、`mode=repair|refresh`；复用 `/api/task/status` 和 `/api/task/stop`。
- `POST /api/archive/articles/open-reader`：按 `articleId` 打开本地文件，不增加归档文件的 HTTP 服务。

阶段标识为 `detail`、`comments`、`avatars`、`body_images`、`reader`、`offline`。

## 验证

在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

前端在 `vue-project` 中运行 `pnpm run type-check` 和 `pnpm run build-only`。真实微信窗口的点击、获取新 token 和文章定位需要在 Windows 登录桌面验证；离线阅读可以在浏览器阻断所有 HTTP/HTTPS 后验证。

已有重建页可使用 `python scripts/check_reader_browser.py "文章目录" --screenshot screenshot.png` 验证。脚本通过 Playwright 打开本地文件，阻断 HTTP/HTTPS，检查所有实际图片可解码，并统计留言、回复及远程请求数。
