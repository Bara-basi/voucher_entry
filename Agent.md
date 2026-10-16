## 一句话描述项目目标
这是一共凭证录入项目，目标是代替财务人员机械重复的从记录到凭证的录入工作。我们的数据来源是飞书的多维表格，需要综合`迈拓财务部门数据`和`2026年报关 AI`表的数据，将数据转换为凭证导入模板，提供给财务人员手动导入即可。
## 项目架构
fastapi后端，后端代码统一用python
vue前端


## 代码范式
1. 临时脚本放在tmp/，服务写在app/services/，特殊功能或非服务脚本可以放到scripts/下，禁止大量创建新文件夹和文件，临时脚本和日志需同步写入.gitignore，临时文件在确认无用后必须删除，保持工作台整洁
2. 下载等长线任务必须连带断点接续功能

## 其它规范
工作台代码全部为UTF-8,不要使用GBK读取


## 审查模型范式
When generating structured output, never translate enum values.
Always preserve exact schema literals such as "allow" and "deny".
非审查模型默认语言使用中文。

## 信息传递范式
1. 每次任务后，如果你创建了新文件或者删除了旧文件，需要在最终聊天告知我文件路径和含义