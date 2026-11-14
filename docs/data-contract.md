# 资料与统计口径

调查按旧边界 `surveys.area` 记录，`wave` 为两次独立横截面；`need` 为是否需要上门服务，`weight` 为原调查权重。`population` 有新旧边界下的年龄、家庭结构和行动便利程度交叉单元；`cell_status=impossible` 是结构零，`unknown` 是缺口，只有 `observed` 能按实际数解释。`crosswalk` 每行是旧区流向新区的人口比例允许范围，同一旧区需满足总流向为1，不直接把范围中点当确定事实。`controls.total_need=null` 表示没有已知上级需求总数，不能凭空添加约束；非空时表示同口径总量。全无问卷地区以及低样本地区确实存在。现行浏览未做借用信息估计，新增模型必须区分直接样本与后分层外推。

## 原始表

- `surveys`：456条示例，主键为 `response`。字段：`response`, `wave`, `area`, `age`, `living`, `mobility`, `need`, `weight`。
- `population`：192条示例，主键为 `cell`。字段：`cell`, `boundary`, `area`, `age`, `living`, `mobility`, `count`, `cell_status`。
- `crosswalk`：14条示例，主键为 `mapping`。字段：`mapping`, `old_area`, `new_area`, `low`, `high`。
- `controls`：2条示例，主键为 `wave`。字段：`wave`, `total_need`, `population_version`。

## 版本与扩展

`fixtures/data.json` 是完整导入材料，`product.json` 是当前浏览器默认选择。全量导入校验后按规范化内容指纹保存，相同内容不重复建版本。原始事实不原地修改；资料修订形成新版本。`null` 表示明确未观测，不因筛选或合计自动变为0。

`neighborhood_need/domain.py` 是领域入口，现阶段仅做字段校验及资料描述；`service.py` 连接范围选择、数据版本与服务端计算后的存档；`storage.py` 管理不可变数据和保存材料；`web/app.js` 提供浏览和导出交互。保存的材料同时包含原数据与选择，打开旧材料不重算为新数据。新增分析不限内部组织方式，但不能丢失现有原始记录核对与历史材料读取能力。统计方法、模型选择和业务结论没有预设答案。
