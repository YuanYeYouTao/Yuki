# SQLite 全仓三轮审查

审查时间：2026-10-22至23。第1、2轮基线为`cdb49eadc0c7cd16060bf0329234ae69611e62bd`；
审查期间main更新，文档分支已ff同步到`c2ce7da66ab54efe192993b9615df48266a62137`。
第3轮在新基线上收尾，三位交叉审查人各自检查36文件差异，原两轮报告保留当时基线。
本目录记录源码和设计审查；产品重构、项目测试、性能对照和部署尚未进行。

主文档：[统一写入与短事务重构任务书](../Yuki-SQLite统一写入与短事务重构任务书-2026-10-22.md)。

## 完整范围

使用git跟踪文件清单、全Python AST、SQL字面量/拼接模板、Schema定义、引用和运行时装配
交叉查找。源代码和测试不以搜索命中作为入选前提：全部进入分类；外部锁定参与库另列。

| 范围 | 清单数量 | 审查方式 |
| --- | ---: | --- |
| src全部Python源码 | 647 | A239、B87、C211、D110；逐文件分类，DB实现/组合调用/恢复消费者语义审查 |
| migrations | 99 | 97个revision+env+template；完整链、所有DDL/DML/导入、当前head0147与离线边界 |
| tests与support材料 | 496 | 全清单和函数/引用分类；重点真实WAL/取消/回执/隐私/恢复测试正文，具体等级见各轮报告；未执行项目测试 |
| 操作与配置材料 | 24 | 连接/升级/部署/副本/打包入口；历史schema评估脚本不当Bot writer |
| 历史实验Python | 18 | 保留导入/连接/HTTP调用边界；不执行历史或付费探针，不当现行Bot模块迁移 |
| 插件与示例 | 9 | 7插件+2示例Python；SDK间接KV/config/publication链也在审查范围 |
| 历史实验数据 | 5 | 数据，不是执行代码；按实际内容排除运行时迁移 |
| 锁定参与库 | 1个外部实现域 | 新pin`addfcdde`的SnapshotStore与宿主AsyncSnapshotStore；独立核两pin的store.py无diff，保持专用线程/CAS |

1298项是源码及支持材料清单。报告分别记录全文、指定数据库方法、接口/间接调用、Schema/
离线和纯类型/计算排除；静态信号仅作导航。artifact_refs拼接触发器、Automation完整领域、
AdminQueries/EventLedger等此前只作结构分类的方法，均在后两轮或D跨域补读中具名补齐。

## 三轮安排与记录

每轮覆盖同一全范围清单，视角不同；A/B/C轮转审查人，D由主审连续核查并接受跨域挑战。

| 轮次 | A：身份/公共历史/控制面/适配/独立小库 | B：Memory全链 | C：Work/模型/Social/插件/Sandbox/Services | D：装配/Schema/工具/全局 |
| --- | --- | --- | --- | --- |
| 1 资源、入口与实际读写 | [review-a](review-a.md)，完成 | [review-b](review-b.md)，完成 | [review-c](review-c.md)，完成 | [review-d §1](review-d.md)，完成 |
| 2 事务语义、隐藏写、原子性与反例 | [round-2-a](round-2-a.md)，完成 | [round-2-b](round-2-b.md)，完成 | [round-2-c](round-2-c.md)，完成 | [review-d 第2轮](review-d.md#第-2-轮反例语义与全局消费者)，完成 |
| 3 交错、删减、性能与迁移终审 | [round-3-a](round-3-a.md)，完成 | [round-3-b](round-3-b.md)，完成 | [round-3-c](round-3-c.md)，完成 | [review-d 第3轮](review-d.md#第3轮全局交错遗漏补读删减与新main差异)，完成 |

第1轮清点资源与实际入口，第2轮换人检查原子性和隐藏调用，第3轮补累计遗漏、端到端交错与
新main差异。A/B/C每轮独立记录阅读等级；D附110文件累计等级及第3轮动作，并列出接手的
5项A域补读。纯类型/格式代码保结构排除，重点测试阅读与实际运行分开记录。

前轮报告保留当时提出的待补项，第3轮与D15记录其实际补齐范围。A/B/C三轮各自都有完整
239/87/211路径表；D的110路径表按累计等级和末轮动作列明，三轮正文分别保留。

## 已改变的设计

1. 同一个主库writer统一，独立库按真实文件、进程与已有owner分别处理。
2. 收起公开session factory，读入口不自动commit；保已有一致读和writer autoflush。
3. pre_admit可能创建身份、Dream.ready_preview会标stale、独立库首读会建表，按真实效果分类。
4. 原子单位按领域区分：Memory逐claim、自省逐结果、Dream逐cluster、插件publication整组；不一刀切合批。
5. 平台已发后的本地补记、unknown commit、跨库反馈和原paid结果按原身份恢复；不新建提交账本。
6. 删除旧BUSY升级循环、first-write标志、重复owner/事务包装；再删旧holders观测与专用HintSession。
7. 保正常连接复用、纯读快路、现有预分词和分页；同到达负载测响应/吞吐/队列，不以少告警冒充提速。
8. unknown沿HTTP/工具typed result/SDK/Work保uncertain=True与commit=None；不可被默认错误映射判成确定未写。
9. 人物forget/merge、配置rollback谱系、跨入口Work来源和Self混合恢复按真实局部依赖复核，不用全库版本挡无关变化。

主任务书现为35个迁移单元、73项实施任务、50组行为场景，包含方法范围、依赖、原子单位、
锁序、删除项、部署边界与性能对照。用户确认当前延迟可接受；保住现有速度是设计目标。

## 取证材料与复现

- [surface.json](surface.json)：每文件导入、候选调用的行号、SQL头和分类信号，不运行Yuki。
- [schema.json](schema.json)：所有migration的revision/导入/函数调用/SQL，及217处ORM/Core/SQL定义。
- [collect_surface.py](collect_surface.py)、[collect_schema.py](collect_schema.py)：只读git与AST；输出到本目录，不连接业务数据库。
- [validation.json](validation.json)：逐轮路径无缺项、35/73/50编号无重复遗漏、任务依赖无环、97revision无缺父且head0147、文档本地链接及空白检查结果。

两个取证脚本的Ruff检查和格式检查通过，重建材料时未发生Python解析错误。材料校验只检查
审查交付的一致性；本次没有运行项目测试或性能负载，未提交、推送、合并或部署。

在该worktree下使用Python3.12运行两个collect脚本即可重建静态材料。它们不证明函数真的写库，
不作为运行时门禁，也不强迫未来保持固定文件数量。三轮发现与最终裁决保留在本目录，旧事故
数字仍见[当日调查](../../operations/sqlite-write-contention-20261022.zh-CN.md)。
