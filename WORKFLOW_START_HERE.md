# 从这里开始：TCAD 工作流程

工作区使用 `D:/onedrive/Desktop/TCAD_Work`。在 VS Code 的 File → Open Folder 打开它；在 Codex 项目选择同一文件夹。

## 日常三步

1. 复制 `tasks/_template`，写清问题、工程、节点、允许修改和输出。当前八节点批次说明在 `tasks/EXP-001`。
2. 在 Codex 明确要求执行指定任务；只保存任务文件不会自动运行。修改原工程输入需要在任务中明确授权。
3. 查询或 refresh 运行编号，查看 `reports` 中的 REPORT.md / report.html。刷新报告不会提交仿真。

Git 保存版本和差异；运行记录保存命令、退出码、日志和产物。手动 SWB 操作请补充到 progress.md。

## 当前运行

编号：`sdevice1_20261022T113935Z_d2b4223f`。

它是此前已提交的副本批次，继续按原路径查询；新任务默认使用指定原工程。不要为“继续”旧任务执行 prepare/submit。

## PowerShell 命令

```powershell
$tcadPython = 'C:/Users/30757/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$tcadTool = 'D:/onedrive/Desktop/TCAD_Work/project_notes/server_tools/tcad_workflow.py'
$tcadRun = 'sdevice1_20261022T113935Z_d2b4223f'
& $tcadPython -B $tcadTool status --run-id $tcadRun
& $tcadPython -B $tcadTool refresh --run-id $tcadRun
```

每次 refresh 新建报告目录，不覆盖历史证据。服务器每分钟记录状态，本机按需回收；HTML 是有时间戳的静态快照。

## GitHub 范围

仅发布允许的工具、任务记录、CSV、图和报告。私钥、许可证、官方手册/例子、大型 TDR、原始日志和本地依赖不上传。用户已指定现有 `origin`：horsepointelciene-sudo/sentaurus。仅 reports/public 中的脱敏报告参与上传。

工具细节、失败处理和验证范围见 `project_notes/server_tools/WORKFLOW_V2.md`。

## 本轮验证状态

历史节点 5 的曲线通过 SVisual CSV 交叉校验。空间极值与两器件截线已由 SVisual 实际导出；完整二维图因服务器自带 Xvfb 与系统 GLIBC 不兼容暂未完成，报告保留 GUI 导出指引。没有启动新的求解。

## 文件为什么显示 U，如何查看上传范围

VS Code 中的 `U` 表示文件尚未加入 Git 的跟踪；并不表示已经传到 GitHub。流程是“加入暂存 → 提交 → 推送”。

本仓库的 `.gitignore` 使用允许清单：根目录只收录工作流说明，其他 PDF、Word、PPT、个人图片和散落 CSV 均留在本机；server_tools 只收录列明的脚本，projects 只收录经过核验的四个输入记录文件，reports 只收录 public 中的精简报告。忽略文件不会删除文件。

在 TCAD_Work 的终端查看：

```powershell
git status --short
git ls-files
git diff --cached --stat
```

准备好本轮内容后，先执行 `tcad_publish.py --report <本地报告目录>` 生成公开版，再仅加入需要发布的文件，执行 `tcad_publish.py --audit-staged` 检查暂存内容，最后提交并 `git push origin main`。普通 `git push` 不会上传工作目录中未跟踪或被忽略的文件。不要用 `git add -f` 强制加入私人资料。
