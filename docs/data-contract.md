# 资料与统计口径

调查按旧边界 `surveys.area` 记录，`wave` 为两次独立横截面；`need` 为是否需要上门服务，`weight` 为原调查权重。`population` 有新旧边界下的年龄、家庭结构和行动便利程度交叉单元；`cell_status=impossible` 是结构零，`unknown` 是缺口，只有 `observed` 能按实际数解释。`crosswalk` 每行是旧区流向新区的人口比例允许范围，同一旧区需满足总流向为1，不直接把范围中点当确定事实。`controls.total_need=null` 表示没有已知上级需求总数，不能凭空添加约束；非空时表示同口径总量。全无问卷地区以及低样本地区确实存在。

## 原始表

- `surveys`：456条示例，主键为 `response`。字段：`response`, `wave`, `area`, `age`, `living`, `mobility`, `need`, `weight`。
- `population`：192条示例，主键为 `cell`。字段：`cell`, `boundary`, `area`, `age`, `living`, `mobility`, `count`, `cell_status`。
- `crosswalk`：14条示例，主键为 `mapping`。字段：`mapping`, `old_area`, `new_area`, `low`, `high`。
- `controls`：2条示例，主键为 `wave`。字段：`wave`, `total_need`, `population_version`。

## 小域需求估计口径

`POST /api/estimate` 在服务端完成小域估计（MRP：多层回归与后分层），前端不另算统计答案：

1. **模型**：个人层加权 logistic 混合模型。固定效应为截距、年龄 older、家庭 alone、行动 limited 及 older×limited、alone×limited 交互；旧社区随机截距吸收社区间差异。固定效应带 N(0, 2.5²) 弱信息高斯先验处理稀疏单元（如 older×free 仅1份问卷的拟分离）；随机效应标准差 sigma 带 LogNormal(中位数0.5, sd_log=0.5) 先验，防止社区数少时方差后验众数塌缩到0。先验参数随结果 `model` 段输出。
2. **推断**：Laplace 近似得到联合后验协方差，蒙特卡洛抽样传播固定效应、sigma 与随机截距不确定性。无样本社区的随机截距没有似然贡献，只从边际先验 `u = Z·sigma` 抽样，结果永久标记为“无本地问卷、完全借用”，`direct` 为 `null`，绝不包装成直接证据。
3. **后分层**：在人口交叉单元上 `人数 = 单元人口 × 模型需求率` 求和。`impossible` 结构零贡献恒为0；`unknown` 缺口按情景策略处理（均值插补并抽样/保守记0/手工给定），处理方式逐单元写入 `cells[].treatment`。全部运算非负；上级总量校准时只用正的乘法因子，不做减法摊派，因此局部人数不可能为负。
4. **边界映射**：新边界社区的随机效应与直接率由 `crosswalk` 流向混合。随机流向在可行域（low/high 且总和1）内抽样；`crosswalk_impact` 枚举各旧社区可行流向多胞形的全部顶点，固定模型与人口点参数，隔离出边界映射本身的影响范围。对称区间角点退化时显式说明，而不是隐藏。
5. **可靠程度**：`高/中/低` 规则写入 `meta.reliability_rules`；`borrowed_share` 是随机截距中先验精度的权重，1.0 表示完全借用。每个社区附 `dependency`（来源旧区、流向中点、来源样本量、来源借用占比）与涉及的不确定映射。
6. **留出检验（LOCO）**：每次完整留出一个社区的全部问卷重新拟合，预测该社区；不做同社区随机拆条。并排报告 MRP、仅固定效应后分层（adjusted_poststratification）、街道均值（street_average_benchmark）三套口径的 bias/MAE/RMSE 与95%区间覆盖率。
7. **总量关系**：`total.need_count` 恒等于各社区模型人数之和，`basis` 说明是否经过上级总量校准。边际段按年龄、家庭结构、行动程度给出同口径分项，分项之和与总量一致。

## 人口情景

请求 `scenario` 全部字段可选，服务端校验并回传补全后的情景（含指纹 `audit.scenario_fingerprint`）：

- `population_multiplier`（≥0）与 `area_multipliers`（按社区，≥0）：排班人口情景系数。
- `unknown_policy`：`average`（默认，本社区已统计单元均值+对数正态抽样）、`conservative`（缺口记0并提示补统）、`manual`（必须通过 `unknown_counts` 以 `社区|年龄|家庭|行动` 为键补齐全部缺口）。
- `crosswalk_position`：null 时每次在可行域随机抽样；0至1时按位置取确定流向。
- `monte_carlo_draws`（50至2000，默认500）、`random_seed`（同种子可复现）、`loco`（布尔，默认开）。

## 版本与扩展

`fixtures/data.json` 是完整导入材料，`product.json` 是当前浏览器默认选择。全量导入校验后按规范化内容指纹保存，相同内容不重复建版本。原始事实不原地修改；资料修订形成新版本。`null` 表示明确未观测，不因筛选或合计自动变为0。

`neighborhood_need/domain.py` 保留字段校验及资料描述浏览；`estimation.py` 是小域估计的唯一统计入口；`service.py` 连接范围选择、情景、数据版本与服务端计算后的存档；`storage.py` 管理不可变数据和保存材料；`web/app.js` 提供浏览、估计、情景调整与导出交互。保存的材料同时包含原数据与选择/情景，打开旧材料不重算为新数据。新增分析不丢失现有原始记录核对与历史材料读取能力。
