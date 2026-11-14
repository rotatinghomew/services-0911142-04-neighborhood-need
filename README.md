# 街区服务需求估计站

这是供服务规划人员核对资料、查看业务范围和留存讨论依据的本地应用。当前提供街区样本与人口交叉单元浏览，并在同一口径下提供小域需求估计（多层回归+后分层）：输出各社区需求人数、95%区间与可靠程度，与问卷直接估计并排；支持人口情景调整、借用信息来源追溯、边界映射角点影响范围和整社区留出检验。示例为独立生成的虚构数据，不含真实家庭、医疗资料或对外服务凭据。

## 使用环境

使用 Python 3.11，独立容器镜像锁定到 3.11.14。页面为原生浏览器界面，数据处理通过本地 HTTP 服务完成，持久化使用应用内置数据库。基础浏览不依赖数值库；研究扩展所需的数值计算和建模库已按 `requirements.txt` 固定所有传递版本。依赖安装及镜像构建只在环境准备时做，启动和检查不访问网络。

```bash
python3 -m pip install -r requirements.txt -r requirements-build.txt -r requirements-test.txt
python3 -m playwright install --with-deps chromium
python3 -m neighborhood_need --host 127.0.0.1 --port 8080
```

浏览器打开本地8080端口。容器方式先准备镜像，运行时不需要其他服务：

```bash
docker build -t neighborhood_need:local .
docker run --rm -p 8080:8080 -v neighborhood_need-state:/app/.state neighborhood_need:local
```

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 tests/browser_smoke.py
```

检查包含真实领域字段、非法导入、边界数据保留、HTTP交互、版本指纹及重启后存档，并覆盖小域估计的统计识别：无样本社区不得出现直接证据、人数与区间非负、结构零与缺口区分、情景非负校验、整社区留出检验、角点影响枚举和口径汇总关系。新增能力需要补充针对统计识别、支持范围和页面一致性的回归。运行检查使用临时状态，不改动资料。

字段语义与现有接口分别见 `docs/data-contract.md` 和 `docs/http.md`。短时操作见 `docs/walkthrough.md`，环境准备不包含在正式操作中。
