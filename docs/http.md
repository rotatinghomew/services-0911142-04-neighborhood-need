# 应用接口

服务绑定本地地址，输入输出为 UTF-8 JSON。错误输入为400，未找到对象为404，超过8MB请求为413。页面与服务端共用口径，前端不另算一份统计答案。

- `GET /health`：检查启动是否成功。
- `GET /api/context`：产品配置、默认数据和已保存材料目录。
- `GET /api/data?revision=版本`：查看完整原始表。
- `POST /api/datasets`：传入 `label` 与完整 `data`，校验后返回不可变 `revision`。
- `POST /api/explore`：传入可选的 `revision` 和 `selection`，返回实际选择、资料指纹与描述性 `view`。该接口保持原有行为，只做并排描述，不做需求估计。
- `POST /api/estimate`：传入可选的 `revision`、`selection` 与 `scenario`，返回小域需求估计。`view.kind` 固定为 `small_area_need_estimate`，包含模型参数与先验、各社区模型/直接并排估计与可靠程度、单元级追溯、边际分项、crosswalk 角点影响、LOCO 检验、总量口径与双指纹。非法情景（负系数、手工缺口未补齐、抽样次数越界等）返回400。
- `POST /api/notebooks`：传入选择与 `title`，由服务端重新生成结果后保存，不接受客户端伪造结果。请求带 `kind:"estimate"` 时按 `/api/estimate` 口径计算并冻结情景；否则保持原描述口径。
- `GET /api/notebooks` 与 `GET /api/notebooks/编号`：目录以及冻结资料、选择、情景和结果。估计记录的判别方式是 `result.view.kind == "small_area_need_estimate"`。

新增分析不改变已有导入、描述和历史读取的含义；同一 `revision + selection + scenario` 在固定种子下可复现。
