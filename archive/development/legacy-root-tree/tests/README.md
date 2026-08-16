# “策联杯”B题 LaTeX 论文模板

本模板按赛事格式规范和团队技术路线预置，适用于“海上油田人员直升机运载计划编排”。正文采用“统一模型与校验器 → Q1 ALNS → Q2 PDVRP-ALNS → Q3 路线生成与 CP-SAT 排班 → 验证与敏感性”的叙事结构。

## 编译

推荐在 Overleaf 或安装完整 TeX Live 的电脑上使用 XeLaTeX：

```bash
latexmk -xelatex -bibtex main.tex
```

也可依次运行：

```bash
xelatex main.tex
bibtex main
xelatex main.tex
xelatex main.tex
```

模板使用 `ctexart`，请勿用 pdfLaTeX。若本机缺少 `ctex`，安装完整版 TeX Live，或在 Overleaf 中选择 XeLaTeX。

## 已固化的官方要求

- 第 1 页只放标题、中文摘要和关键词，页码从 1 开始并位于页脚中部；
- 第 2 页开始正文，不生成目录；
- 正文不超过 30 页，附录页数不限；
- 全文不设置作者、单位、学校等身份字段；
- “AI 工具使用声明”位于参考文献之前；
- 附录包含支撑材料清单、完整源程序入口和提交前检查表；
- 提醒论文 PDF 与支撑材料分别不超过 20 MB。

## 比赛时的推荐用法

1. 成员 C 从第一小时维护 `main.tex`，不要等算法全部完成才开始写。
2. 成员 A 将程序生成的图保存为 `figures/*.pdf`，文件名与模板中的引用保持一致。
3. 结果表最好由 Python 自动导出为 `tables/*.tex`，再用 `\input{tables/xxx.tex}` 引入，避免手工改数字。
4. 所有五项指标只从最终 CSV 经独立 Validator 复算，不从优化器内部缓存复制。
5. 提交前运行下列搜索，确保没有遗留占位符：

```bash
rg -n '待填|\\todo|\\metric|--' main.tex
```

6. 在 PDF 中人工确认：摘要确实只有一页；正文从第 2 页开始；没有目录；AI 声明位于参考文献之前；附录之前的总页数不超过 30。

## 图片文件名

- `figures/framework.pdf`：统一求解框架；
- `figures/q2_route_state.pdf`：代表性混合架次的上下客、载荷与油量；
- `figures/q3_gantt.pdf`：代表性飞机的多日甘特图。

图片不存在时模板会显示占位框，便于先写论文；正式提交前必须全部替换。

## 参考文献

`references.bib` 仅预置车辆路径、节约算法和 ALNS 的经典文献。只保留正文实际引用且已人工核对的条目，并增补比赛期间真实查阅的资料。

## 附录代码

模板通过 `\inputcode` 引入 `../src/` 中的完整源程序。若你们的工程目录不同，应统一修改路径。缺少文件时 PDF 会显示红色警告；正式提交前所有警告都必须消失。
