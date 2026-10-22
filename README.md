# Sentaurus TCAD 项目记录

用 Git 保存研究问题、输入版本、运行证据、图表与报告。此仓库对应 `horsepointelciene-sudo/sentaurus`，按用户要求公开发布。

- [从基础开始使用](WORKFLOW_START_HERE.md)
- [当前任务与授权范围](tasks/EXP-001/task.md)
- [公开版结果和报告](reports/public/README.md)
- [运行工具和验证说明](project_notes/server_tools/WORKFLOW_V2.md)
- [已核验的历史输入副本](projects/4E17SPICE_1.18u/INPUTS.json)

已实现原工程运行入口、任务互斥、必要备份与归档、每分钟服务器监控、曲线校验、科研图及 Markdown/HTML 报告。现有八节点任务继续按原运行编号查询，不因部署工具而重跑。

历史节点 5 的曲线和 SVisual 数值导出已交叉验证。两器件场量极值和截线图已经生成；完整二维场图暂受服务器 Xvfb/GLIBC 兼容性限制，未宣称完成。

本仓库不包含 Synopsys 软件、专有手册/例子、许可证、SSH 凭据、原始日志、大型网格或完整材料参数。复现实验仍需要已授权的 Sentaurus 环境和本地保留的输入；连接配置 `tcad_118u.json` 仅留在本机。
