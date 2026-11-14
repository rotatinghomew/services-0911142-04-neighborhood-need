"""小域需求估计：多层回归与后分层（MRP）。

设计口径（与 docs/data-contract.md 对应）：

* 调查按旧边界记录，模型在个人层拟合加权 logistic 混合模型，固定效应使用
  年龄、家庭结构、行动便利程度及其与行动程度的交互，旧社区随机截距吸收
  社区间差异；Laplace 近似给出参数后验协方差，蒙特卡洛抽样传播不确定性。
* 没有任何问卷的社区，其随机截距没有似然贡献，只从 N(0, sigma^2) 先验抽取，
  结果永远标记为“无本地样本、纯借用”，不伪装成直接证据。
* 后分层严格在人口单元上进行：impossible 是结构零，贡献恒为 0；unknown 是
  缺口，按情景策略处理并单独披露，绝不自动当 0 事实。
* 新边界社区的随机效应由 crosswalk 流向混合得到；流向只有区间时，每次蒙特
  卡洛在可行域内抽样，并另外给出全低/全高角点的影响范围。
* 所有单元人口与人数只做非负运算；上级总量校准只用正的乘法因子，不做减法
  摊派，因此不会出现负的局部人数。
"""
from itertools import combinations, product
import math

import numpy as np

from .validation import finite
from .storage import fingerprint

TERMS = ["截距", "年龄=older", "家庭=alone", "行动=limited", "older×limited", "alone×limited"]
CELL_KEYS = ("age", "living", "mobility")
DEFAULT_DRAWS = 500
LOCO_DRAWS = 200
DEFAULT_SEED = 20261114
# 固定效应弱信息先验 N(0, FIXED_PRIOR_SD^2)：处理 sparse 单元（如 older×free 仅1份）
# 的拟分离，避免系数发散把随机效应方差压为0；先验强度随结果输出，可复核可替换。
FIXED_PRIOR_SD = 2.5
# 随机效应标准差 sigma 的弱信息先验 LogNormal(log SIGMA_PRIOR_MEDIAN, SIGMA_PRIOR_SDLOG^2)。
# 只有11个社区时，方差的边际后验众数会退化到0（Gelman 2006 指出 HalfNormal 在
# 组数少时仍允许 sigma→0）；较紧的 log 空间先验在 sigma 很小时给出 log(1/sigma)
# 障碍，保证无样本社区的先验预测分布保留有意义的社区间差异，而不是退化为点。
SIGMA_PRIOR_MEDIAN = 0.5
SIGMA_PRIOR_SDLOG = 0.5
UNKNOWN_POLICIES = ("average", "conservative", "manual")
# 可靠程度分级规则，同时写入输出 meta，供页面与文档引用
RELIABILITY_RULES = {
    "高": "有本地样本且模型估计95%区间半宽不超过点估计的20%",
    "中": "有本地样本且模型估计95%区间半宽不超过点估计的40%",
    "低": "区间更宽，或本社区没有问卷样本（完全依赖借用信息）",
}


def _native(value):
    """把 numpy 标量/数组转成可 JSON 序列化的原生类型，并拒绝 NaN。"""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("估计出现非有限数值，不能序列化")
        return value
    if isinstance(value, (np.ndarray, list, tuple)):
        return [_native(item) for item in value]
    if isinstance(value, dict):
        return {key: _native(item) for key, item in value.items()}
    if isinstance(value, float):
        raise ValueError("估计出现非有限数值，不能序列化")
    return value


def _softplus(value):
    return np.logaddexp(0.0, value)


def design_vector(row):
    older = 1.0 if row["age"] == "older" else 0.0
    alone = 1.0 if row["living"] == "alone" else 0.0
    limited = 1.0 if row["mobility"] == "limited" else 0.0
    return np.array([1.0, older, alone, limited, older * limited, alone * limited])


def validate_selection(data, selection):
    waves = sorted({row["wave"] for row in data["surveys"]})
    boundaries = sorted({row["boundary"] for row in data["population"]})
    if not isinstance(selection, dict) or selection.get("wave") not in waves or selection.get("boundary") not in boundaries:
        raise ValueError("选择范围无效：wave/boundary 必须来自现有资料")


def validate_scenario(data, scenario):
    """校验排班人员可调整的人口情景，返回补全默认值后的情景。"""
    scenario = dict(scenario or {})
    multiplier = scenario.get("population_multiplier", 1.0)
    finite(multiplier, minimum=0.0)
    area_multipliers = scenario.get("area_multipliers", {})
    if not isinstance(area_multipliers, dict):
        raise ValueError("社区人口调整系数必须是对象")
    areas = {row["area"] for row in data["population"]}
    for area, value in area_multipliers.items():
        if area not in areas:
            raise ValueError("人口调整引用了不存在的社区：" + str(area))
        finite(value, minimum=0.0)
    policy = scenario.get("unknown_policy", "average")
    if policy not in UNKNOWN_POLICIES:
        raise ValueError("缺口单元策略无效，可选：" + "/".join(UNKNOWN_POLICIES))
    unknown_cells = {
        (row["area"], row["age"], row["living"], row["mobility"])
        for row in data["population"] if row["cell_status"] == "unknown"
    }
    manual = scenario.get("unknown_counts", {})
    if not isinstance(manual, dict):
        raise ValueError("手工缺口人口必须是对象")
    manual_resolved = {}
    for key, value in manual.items():
        parts = str(key).split("|")
        if len(parts) != 4:
            raise ValueError("缺口单元键格式应为 社区|年龄|家庭|行动：" + str(key))
        cell = tuple(parts)
        if cell not in unknown_cells:
            raise ValueError("只能为 unknown 缺口单元提供手工人口：" + str(key))
        finite(value, minimum=0.0)
        manual_resolved[cell] = float(value)
    if policy == "manual" and unknown_cells - set(manual_resolved):
        raise ValueError("手工策略要求补齐全部 unknown 单元的人口")
    position = scenario.get("crosswalk_position", None)
    if position is not None:
        finite(position, minimum=0.0, maximum=1.0)
    draws = int(scenario.get("monte_carlo_draws", DEFAULT_DRAWS))
    if not 50 <= draws <= 2000:
        raise ValueError("蒙特卡洛次数应在50至2000之间")
    seed = int(scenario.get("random_seed", DEFAULT_SEED))
    if not 0 <= seed <= 2**32 - 1:
        raise ValueError("随机种子超出范围")
    if not isinstance(scenario.get("loco", True), bool):
        raise ValueError("留出社区校验开关必须为布尔值")
    return {
        "population_multiplier": float(multiplier),
        "area_multipliers": {key: float(value) for key, value in area_multipliers.items()},
        "unknown_policy": policy,
        "unknown_counts": {"|".join(key): value for key, value in manual_resolved.items()},
        "crosswalk_position": None if position is None else float(position),
        "monte_carlo_draws": draws,
        "random_seed": seed,
        "loco": scenario.get("loco", True),
    }


def crosswalk_groups(data):
    """按旧社区整理流向，返回 {old_area: [mapping rows]} 并校验可行性。"""
    population_areas = {row["area"] for row in data["population"]}
    groups = {}
    for row in data["crosswalk"]:
        if row["old_area"] not in population_areas or row["new_area"] not in population_areas:
            raise ValueError("流向表引用了人口表中不存在的社区")
        if not 0.0 <= row["low"] <= row["high"] <= 1.0:
            raise ValueError("流向比例超出0至1范围：" + row["mapping"])
        groups.setdefault(row["old_area"], []).append(row)
    old_boundary_areas = {row["area"] for row in data["population"] if row["boundary"] == "old"}
    if set(groups) != old_boundary_areas:
        raise ValueError("流向表必须覆盖全部旧边界社区，且不能多余")
    for old_area, rows in groups.items():
        low_sum = sum(row["low"] for row in rows)
        high_sum = sum(row["high"] for row in rows)
        if low_sum > 1.0 + 1e-9 or high_sum < 1.0 - 1e-9:
            raise ValueError("流向比例不存在总和为1的可行组合：" + old_area)
        targets = [row["new_area"] for row in rows]
        if len(targets) != len(set(targets)):
            raise ValueError("同一旧社区流向目标重复：" + old_area)
    return groups


def _feasible_flows(rows, rng, fixed_position=None):
    """为一个旧社区抽取一组总和为1且落在 low/high 区间内的流向。

    fixed_position 给定时在低/高顶点之间按比例取点并归一修正；不给定时在
    可行域内随机抽样。严格的角点枚举见 _flow_vertices。
    """
    lows = np.array([row["low"] for row in rows])
    highs = np.array([row["high"] for row in rows])
    if fixed_position is not None:
        if len(rows) == 1:
            return np.array([1.0])
        flows = lows + fixed_position * (highs - lows)
        gap = 1.0 - flows.sum()
        if abs(gap) > 1e-12:
            room = np.where(gap > 0, highs - flows, flows - lows)
            if room.sum() <= 1e-15:
                flows = flows / flows.sum()
            else:
                flows = flows + gap * room / room.sum()
        return flows
    if len(rows) == 1:
        return np.array([1.0])
    for _ in range(10000):
        candidate = lows + rng.random(len(rows)) * (highs - lows)
        if abs(candidate.sum() - 1.0) <= 1e-9:
            return candidate
    middle = (lows + highs) / 2.0
    return middle / middle.sum()


def _flow_vertices(rows):
    """枚举一个旧社区可行流向多胞形的全部顶点（至少 k-1 个分量贴 low/high）。

    做法：对每个分量依次固定在 low 或 high，其余分量按各自剩余区间比例分配
    残差；去重后即覆盖凸多胞形顶点，规模随分量组合数增长但流向表通常很小。
    """
    lows = np.array([row["low"] for row in rows], dtype=float)
    highs = np.array([row["high"] for row in rows], dtype=float)
    k = len(rows)
    if k == 1:
        return [np.array([1.0])]
    vertices = []

    def add_candidate(fixed):
        flows = np.full(k, np.nan)
        for index, value in fixed.items():
            flows[index] = value
        free = [index for index in range(k) if index not in fixed]
        residual = 1.0 - sum(fixed.values())
        low_room = sum(lows[index] for index in free)
        high_room = sum(highs[index] for index in free)
        if residual < low_room - 1e-10 or residual > high_room + 1e-10:
            return
        if not free:
            if abs(residual) <= 1e-10:
                vertices.append(flows)
            return
        span = high_room - low_room
        if span <= 1e-15:
            for index in free:
                flows[index] = lows[index]
        else:
            share = (residual - low_room) / span
            for index in free:
                flows[index] = lows[index] + share * (highs[index] - lows[index])
        vertices.append(flows)

    add_candidate({})
    for depth in range(1, k):
        for combo in combinations(range(k), depth):
            for signs in product((0.0, 1.0), repeat=depth):
                add_candidate({index: lows[index] + sign * (highs[index] - lows[index])
                               for index, sign in zip(combo, signs)})
    unique = []
    for candidate in vertices:
        if any(np.allclose(candidate, kept, atol=1e-9) for kept in unique):
            continue
        unique.append(candidate)
    return unique


def sample_crosswalk(groups, area_index, rng, fixed_position=None):
    """生成一次抽样的 old×new 流向矩阵 T，T[a, b] 为旧区 a 流向新区 b 的比例。"""
    size = len(area_index)
    matrix = np.zeros((size, size))
    for old_area, rows in groups.items():
        flows = _feasible_flows(rows, rng, fixed_position)
        for row, flow in zip(rows, flows):
            matrix[area_index[old_area], area_index[row["new_area"]]] = flow
    return matrix


def fit_glmm(records):
    """拟合加权 logistic 混合模型，返回 Laplace 近似的后验参数与协方差。

    固定效应带弱信息高斯先验以处理小样本/拟分离单元；随机截距为 N(0,sigma^2)。
    优化使用带边界的 L-BFGS-B，防止 log_sigma 在稀疏数据上数值发散。
    """
    from scipy.optimize import minimize
    areas = sorted({record["area"] for record in records})
    area_index = {area: index for index, area in enumerate(areas)}
    x = np.vstack([design_vector(record) for record in records])
    y = np.array([record["need"] for record in records], dtype=float)
    w = np.array([record["weight"] for record in records], dtype=float)
    group = np.array([area_index[record["area"]] for record in records])
    p_fixed = x.shape[1]
    j_count = len(areas)
    ridge_precision = 1.0 / FIXED_PRIOR_SD ** 2
    prior_center = math.log(SIGMA_PRIOR_MEDIAN)
    prior_precision = 1.0 / SIGMA_PRIOR_SDLOG ** 2

    def unpack(theta):
        beta = theta[:p_fixed]
        log_sigma = theta[p_fixed]
        u = theta[p_fixed + 1:]
        return beta, log_sigma, u

    def neg_posterior(theta):
        beta, log_sigma, u = unpack(theta)
        sigma = math.exp(log_sigma)
        eta = x @ beta + u[group]
        data_term = np.sum(w * (_softplus(eta) - y * eta))
        prior_term = np.sum(u ** 2) / (2.0 * sigma ** 2) + j_count * log_sigma
        sigma_prior = 0.5 * prior_precision * (log_sigma - prior_center) ** 2
        return (data_term + prior_term + 0.5 * ridge_precision * np.sum(beta ** 2)
                + sigma_prior)

    def gradient(theta):
        beta, log_sigma, u = unpack(theta)
        sigma = math.exp(log_sigma)
        eta = x @ beta + u[group]
        prob = 1.0 / (1.0 + np.exp(-eta))
        residual = w * (prob - y)
        grad_beta = x.T @ residual + ridge_precision * beta
        grad_u = np.zeros(j_count)
        for index in range(j_count):
            mask = group == index
            grad_u[index] = residual[mask].sum() + u[index] / sigma ** 2
        grad_log_sigma = (-np.sum(u ** 2) / sigma ** 2 + j_count
                          + prior_precision * (log_sigma - prior_center))
        return np.concatenate([grad_beta, [grad_log_sigma], grad_u])

    theta0 = np.concatenate([np.zeros(p_fixed), [math.log(0.5)], np.zeros(j_count)])
    bounds = [(None, None)] * p_fixed + [(-6.0, 3.0)] + [(None, None)] * j_count
    optimum = minimize(neg_posterior, theta0, jac=gradient, method="L-BFGS-B",
                       bounds=bounds, options={"maxiter": 2000, "ftol": 1e-12, "gtol": 1e-8})
    beta, log_sigma, u = unpack(optimum.x)
    sigma = math.exp(log_sigma)
    eta = x @ beta + u[group]
    prob = 1.0 / (1.0 + np.exp(-eta))
    weight = w * prob * (1.0 - prob)

    hessian = np.zeros((theta0.size, theta0.size))
    hessian[:p_fixed, :p_fixed] = (x * weight[:, None]).T @ x + ridge_precision * np.eye(p_fixed)
    for index in range(j_count):
        mask = group == index
        block = p_fixed + 1 + index
        hessian[:p_fixed, block] = x[mask].T @ weight[mask]
        hessian[block, :p_fixed] = hessian[:p_fixed, block]
        hessian[block, block] = weight[mask].sum() + 1.0 / sigma ** 2
    ell_index = p_fixed
    hessian[ell_index, ell_index] = (2.0 * np.sum(u ** 2) / sigma ** 2
                                     + prior_precision)
    for index in range(j_count):
        block = p_fixed + 1 + index
        hessian[ell_index, block] = -2.0 * u[index] / sigma ** 2
        hessian[block, ell_index] = hessian[ell_index, block]

    eigenvalues, eigenvectors = np.linalg.eigh(hessian)
    ridge = 0.0
    if eigenvalues.min() <= 1e-10:
        ridge = 1e-8 - eigenvalues.min()
        hessian = hessian + np.eye(hessian.shape[0]) * ridge
        eigenvalues, eigenvectors = np.linalg.eigh(hessian)
    covariance = (eigenvectors * (1.0 / eigenvalues)) @ eigenvectors.T
    return {
        "areas": areas, "area_index": area_index, "beta": beta, "log_sigma": log_sigma,
        "sigma": sigma, "u": u, "covariance": covariance, "converged": bool(optimum.success),
        "neg_posterior": float(optimum.fun), "ridge": float(ridge),
        "records": len(records),
    }


def _population_cells(data, boundary):
    cells = [row for row in data["population"] if row["boundary"] == boundary]
    areas = sorted({row["area"] for row in cells})
    return cells, areas


def _unknown_log_tau(cells):
    observed_logs = [math.log(row["count"]) for row in cells if row["cell_status"] == "observed"]
    if len(observed_logs) >= 2:
        return float(np.std(observed_logs, ddof=1))
    return 0.3


def build_population(cells, scenario, rng, draws):
    """返回 (单元矩阵 N[draw, cell], 单元元信息, 分区人口说明)。

    impossible 结构零恒为 0；unknown 缺口按情景策略处理并保留说明，
    observed 使用实际 count；最后统一乘情景系数，全部数值非负。
    """
    counts = np.zeros((draws, len(cells)))
    info = []
    area_observed_mean = {}
    for area in {row["area"] for row in cells}:
        observed = [row["count"] for row in cells
                    if row["area"] == area and row["cell_status"] == "observed"]
        area_observed_mean[area] = sum(observed) / len(observed) if observed else 0.0
    policy = scenario["unknown_policy"]
    manual = {tuple(key.split("|")): value for key, value in scenario["unknown_counts"].items()}
    for index, row in enumerate(cells):
        area_multiplier = scenario["area_multipliers"].get(row["area"], 1.0)
        factor = scenario["population_multiplier"] * area_multiplier
        base = {"cell": "|".join(str(row[key]) for key in ("area",) + CELL_KEYS),
                "area": row["area"], "age": row["age"], "living": row["living"],
                "mobility": row["mobility"], "status": row["cell_status"],
                "observed_count": row["count"], "factor": factor}
        if row["cell_status"] == "impossible":
            base["scenario_count"] = 0.0
            base["treatment"] = "结构零，不分配人口"
        elif row["cell_status"] == "observed":
            counts[:, index] = row["count"] * factor
            base["scenario_count"] = row["count"] * factor
            base["treatment"] = "实际统计人口"
        else:
            cell_id = (row["area"], row["age"], row["living"], row["mobility"])
            if policy == "conservative":
                base["scenario_count"] = 0.0
                base["treatment"] = "缺口按保守策略记0，区间不含该单元，需另行补统"
            elif policy == "manual":
                value = manual[cell_id] * factor
                counts[:, index] = value
                base["scenario_count"] = value
                base["treatment"] = "排班情景手工给定的缺口人口"
            else:
                mean_count = area_observed_mean[row["area"]]
                tau = _unknown_log_tau([item for item in cells if item["area"] == row["area"]])
                sampled = mean_count * np.exp(rng.normal(0.0, tau, draws)) * factor
                counts[:, index] = sampled
                base["scenario_count"] = float(sampled.mean())
                base["treatment"] = "缺口按本社区已统计单元均值插补，抽样反映缺口不确定性"
        info.append(base)
    return counts, info


def direct_estimates(data, wave, scenario_population):
    """旧边界问卷社区的直接（加权）估计；无样本社区返回 None，绝不编造。"""
    records = [row for row in data["surveys"] if row["wave"] == wave]
    result = {}
    for area in sorted({row["area"] for row in records}):
        local = [row for row in records if row["area"] == area]
        n = len(local)
        need_cases = sum(row["need"] for row in local)
        sum_w = sum(row["weight"] for row in local)
        sum_wy = sum(row["weight"] * row["need"] for row in local)
        rate = sum_wy / sum_w
        if n > 1:
            variance = sum(row["weight"] ** 2 * (row["need"] - rate) ** 2 for row in local) / (sum_w ** 2)
            variance *= n / (n - 1)
            se = math.sqrt(max(variance, 0.0))
        else:
            se = None
        effective_n = sum_w ** 2 / sum(row["weight"] ** 2 for row in local)
        population = scenario_population.get(area, 0.0)
        result[area] = {
            "sample_n": n, "need_cases": need_cases, "effective_n": effective_n,
            "rate": rate, "se": se, "count": rate * population,
            "population_basis": population,
            "basis": "本社区旧边界问卷直接加权" + ("（样本极少，仅作对照）" if n < 5 else ""),
        }
    return result


def _poststratify(fit, cells, cell_info, population, crosswalk_matrix, rng):
    """按蒙特卡洛抽样计算每个社区的需求人数后验，返回 draws×社区 与单元贡献。

    无样本社区的随机截距对 sigma 做边际化：u ~ N(0, sigma^2) 与 sigma 的后验
    联合抽样，等价于标准正态乘 sigma，区间反映“社区差异大小本身未知”，而不
    是把 sigma 固定在点值上的条件窄区间。
    """
    areas = sorted({row["area"] for row in cells})
    area_index = {area: index for index, area in enumerate(areas)}
    x_cell = np.vstack([design_vector(row) for row in cells])
    draws = population.shape[0]
    theta_mean = np.concatenate([fit["beta"], [fit["log_sigma"]], fit["u"]])
    theta_draws = rng.multivariate_normal(theta_mean, fit["covariance"], draws)
    beta_draws = theta_draws[:, :fit["beta"].size]
    # Laplace 高斯抽样可能越过优化边界，统一截断到安全区间，避免 sigma 数值溢出
    sigma_draws = np.exp(np.clip(theta_draws[:, fit["beta"].size], -6.0, 3.0))
    u_known = theta_draws[:, fit["beta"].size + 1:]

    full_index = area_index
    u_full = np.zeros((draws, len(areas)))
    for area in areas:
        if area in fit["area_index"]:
            u_full[:, full_index[area]] = u_known[:, fit["area_index"][area]]
        else:
            # 无样本社区：随机截距没有似然，对 sigma 边际化的先验抽样
            u_full[:, full_index[area]] = rng.standard_normal(draws) * sigma_draws
    if crosswalk_matrix is None:
        u_boundary = u_full
    else:
        u_boundary = u_full @ crosswalk_matrix
    logits = beta_draws @ x_cell.T + u_boundary[:, [area_index[row["area"]] for row in cells]]
    probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -60.0, 60.0)))
    need_draws_cells = population * probabilities
    need_draws = np.zeros((draws, len(areas)))
    for index, row in enumerate(cells):
        need_draws[:, area_index[row["area"]]] += need_draws_cells[:, index]
    return need_draws, need_draws_cells, probabilities


def _quantiles(values, q):
    return float(np.quantile(values, q))


def _reliability(has_sample, point, low, high, borrowed):
    if not has_sample:
        return "低"
    if point <= 0:
        return "低"
    relative = (high - low) / 2.0 / point
    if relative <= 0.20 and borrowed <= 0.5:
        return "高"
    if relative <= 0.40:
        return "中"
    return "低"


def crosswalk_corner_impact(data, groups, boundary, fit, cells, scenario, direct_old=None):
    """枚举流向区间角点，给出仅由边界映射不确定性造成的影响范围。

    固定模型点参数与单元人口（取点估计），只变动 crosswalk 角点，从而把边界
    映射的不确定性与模型/缺口不确定性隔离。同时报告两条口径的角点范围：
    模型后分层人数，以及按流向混合的旧社区直接需求率（当新旧人口表一致、点
    随机效应为0时，模型角点差异可能为0，但直接证据的来源占比仍然变化）。
    """
    uncertain = {old: rows for old, rows in groups.items()
                 if any(row["low"] != row["high"] for row in rows)}
    if not uncertain or boundary == "old":
        return {"uncertain": False, "affected_areas": [],
                "note": "所选边界下没有不确定流向，社区随机效应不依赖区间映射。"}
    areas = sorted({row["area"] for row in cells})
    area_index = {area: index for index, area in enumerate(areas)}
    # 每个不确定旧社区枚举可行流向多胞形顶点，再做笛卡尔积；确定社区取唯一可行点
    vertex_options = {old: _flow_vertices(rows) for old, rows in uncertain.items()}
    corner_product = product(*(vertex_options[old] for old in uncertain))
    rng = np.random.default_rng(0)
    point_population, _ = build_population(cells, scenario, rng, 1)
    x_cell = np.vstack([design_vector(row) for row in cells])
    point_logits = fit["beta"] @ x_cell.T
    u_point = np.zeros(len(areas))
    for area in fit["areas"]:
        u_point[area_index[area]] = fit["u"][fit["area_index"][area]]
    per_area_model = {area: [] for area in areas}
    per_area_direct = {area: [] for area in areas}
    involved = {}
    direct_rates = {area: (direct_old[area]["rate"] if direct_old and area in direct_old else None)
                    for area in areas}
    corner_count = 0
    for corner_flows in corner_product:
        corner_count += 1
        if corner_count > 4096:
            break
        vertex_map = dict(zip(uncertain, corner_flows))
        matrix = np.zeros((len(areas), len(areas)))
        for old_area, rows in groups.items():
            if old_area in vertex_map:
                flows = vertex_map[old_area]
            else:
                flows = _flow_vertices(rows)[0]
            for row, flow in zip(rows, flows):
                matrix[area_index[old_area], area_index[row["new_area"]]] = flow
                if row["low"] != row["high"]:
                    involved.setdefault(row["new_area"], set()).add(row["mapping"])
        u_blend = u_point @ matrix
        logits = point_logits + u_blend[[area_index[row["area"]] for row in cells]]
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -60.0, 60.0)))
        need_cells = point_population[0] * probs
        for index, area in enumerate(areas):
            mask = [i for i, row in enumerate(cells) if row["area"] == area]
            per_area_model[area].append(float(need_cells[mask].sum()))
            incoming = matrix[:, index]
            known = [(flow, direct_rates[areas[source]]) for source, flow in enumerate(incoming)
                     if flow > 0 and direct_rates[areas[source]] is not None]
            if known:
                weight_sum = sum(flow for flow, _ in known)
                per_area_direct[area].append(sum(flow * rate for flow, rate in known) / weight_sum)
    affected = []
    for area in areas:
        model_values = per_area_model[area]
        direct_values = per_area_direct[area]
        model_span = max(model_values) - min(model_values)
        direct_span = (max(direct_values) - min(direct_values)) if direct_values else 0.0
        if model_span > 1e-9 or direct_span > 1e-9:
            affected.append({
                "area": area,
                "model_count_low": min(model_values),
                "model_count_high": max(model_values),
                "model_count_zero_span": model_span <= 1e-9,
                "direct_rate_low": min(direct_values) if direct_values else None,
                "direct_rate_high": max(direct_values) if direct_values else None,
                "mappings": sorted(involved.get(area, set())),
            })
    if not affected:
        note = ("枚举全部角点后影响为零：当前流向区间在各目标社区上对称（如低/高互换），"
                "可行角点的混合比例完全相同。映射区间仍然记录在案，若将来区间不对称或"
                "人口表新旧边界出现差异，同一枚举会自动给出非零范围。")
    else:
        note = ("只变动 crosswalk 区间角点、固定模型与人口点参数，隔离边界映射的影响范围；"
                "model_count_zero_span=true 表示当前人口表下模型点估计不受角点影响，但直接证据来源占比仍变动。")
    return {"uncertain": True, "affected_areas": affected, "note": note}


def loco_cross_validation(data, wave, groups, boundary_cells, scenario, seed):
    """逐个完整留出整个社区，用其余社区拟合后预测该社区，禁止随机拆同社区答卷。

    三个预测口径并排检验：
    * mrp_model：完整混合模型，留出社区随机截距取边际先验（模拟无样本社区）；
    * adjusted_benchmark：只用固定效应（年龄/家庭/行动）后分层，借用结构信息但
      不借用社区随机效应，检验“社区层借用”本身是否有增益；
    * street_average_benchmark：街道合并加权率直接套用，即不看人口结构的做法。
    """
    records = [row for row in data["surveys"] if row["wave"] == wave]
    held_areas = sorted({row["area"] for row in records})
    rng = np.random.default_rng(seed + 7)
    rows = []
    for held in held_areas:
        training = [row for row in records if row["area"] != held]
        fit = fit_glmm(training)
        held_cells = [row for row in boundary_cells if row["area"] == held]
        local_scenario = dict(scenario)
        local_scenario["monte_carlo_draws"] = LOCO_DRAWS
        population, _ = build_population(held_cells, local_scenario, rng, LOCO_DRAWS)
        # 留出社区对模型而言没有本地数据，随机截距对 sigma 边际化取先验
        ell = fit["beta"].size
        log_sigma_draws = np.clip(rng.normal(fit["log_sigma"],
                                     math.sqrt(max(fit["covariance"][ell, ell], 0.0)), LOCO_DRAWS),
                                  -6.0, 3.0)
        prior_u = rng.standard_normal(LOCO_DRAWS) * np.exp(log_sigma_draws)
        x_cell = np.vstack([design_vector(row) for row in held_cells])
        beta_draws = rng.multivariate_normal(fit["beta"],
                                             fit["covariance"][:ell, :ell],
                                             LOCO_DRAWS)
        logits = beta_draws @ x_cell.T + prior_u[:, None]
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -60.0, 60.0)))
        model_count_draws = population * probs
        model_rate_draws = model_count_draws.sum(axis=1) / np.maximum(population.sum(axis=1), 1e-9)
        # 调整基准：同一组固定效应、无随机截距的后分层
        adj_logits = beta_draws @ x_cell.T
        adj_probs = 1.0 / (1.0 + np.exp(-np.clip(adj_logits, -60.0, 60.0)))
        adj_count_draws = population * adj_probs
        adjusted_rate_draws = adj_count_draws.sum(axis=1) / np.maximum(population.sum(axis=1), 1e-9)
        direct = direct_estimates(data, wave, {})[held]
        pooled_w = sum(row["weight"] for row in training)
        benchmark_rate = sum(row["weight"] * row["need"] for row in training) / pooled_w
        population_total = population.sum(axis=1)
        rows.append({
            "area": held,
            "direct_rate": direct["rate"],
            "direct_n": direct["sample_n"],
            "model_rate_mean": float(model_rate_draws.mean()),
            "model_rate_q025": _quantiles(model_rate_draws, 0.025),
            "model_rate_q975": _quantiles(model_rate_draws, 0.975),
            "covered": bool(_quantiles(model_rate_draws, 0.025) <= direct["rate"]
                            <= _quantiles(model_rate_draws, 0.975)),
            "adjusted_rate_mean": float(adjusted_rate_draws.mean()),
            "benchmark_rate": benchmark_rate,
            "model_count_mean": float(model_count_draws.sum(axis=1).mean()),
            "adjusted_count_mean": float(adj_count_draws.sum(axis=1).mean()),
            "benchmark_count_mean": float((benchmark_rate * population_total).mean()),
        })

    def metrics(rate_key):
        errors = np.array([row[rate_key] - row["direct_rate"] for row in rows])
        return {
            "bias": float(errors.mean()),
            "mae": float(np.abs(errors).mean()),
            "rmse": float(math.sqrt(np.mean(errors ** 2))),
        }
    coverage = sum(row["covered"] for row in rows) / len(rows)
    model_metrics = metrics("model_rate_mean")
    model_metrics["interval_coverage"] = coverage
    return {
        "held_out_kind": "每次完整留出一个社区的全部问卷，不做同社区随机拆分",
        "draws_per_fold": LOCO_DRAWS,
        "target": "需求率层面的留出预测（直接率 vs 预测率）；新边界下社区人数另由 crosswalk 与人口表后分层得到，不在此重复建模",
        "metrics": {
            "mrp_model": model_metrics,
            "adjusted_poststratification": metrics("adjusted_rate_mean"),
            "street_average_benchmark": metrics("benchmark_rate"),
        },
        "areas": rows,
        "note": "adjusted_poststratification 只用年龄/家庭/行动固定效应做后分层、不借用社区随机效应；"
                "street_average_benchmark 是把街道其余社区的合并加权需求率直接套到留出社区，即不考虑人口结构的做法。",
    }


def estimate(data, selection, scenario=None):
    """主入口：返回供排班讨论的小域需求估计完整结果。"""
    validate_selection(data, selection)
    resolved = validate_scenario(data, scenario)
    groups = crosswalk_groups(data)
    wave = selection["wave"]
    boundary = selection["boundary"]
    rng = np.random.default_rng(resolved["random_seed"])
    draws = resolved["monte_carlo_draws"]

    records = [row for row in data["surveys"] if row["wave"] == wave]
    fit = fit_glmm(records)
    cells, areas = _population_cells(data, boundary)
    population, cell_info = build_population(cells, resolved, rng, draws)

    if boundary == "new":
        crosswalk_matrix = sample_crosswalk(groups, {area: index for index, area in enumerate(areas)},
                                            rng, resolved["crosswalk_position"])
        midpoint_matrix = sample_crosswalk(groups, {area: index for index, area in enumerate(areas)},
                                           np.random.default_rng(resolved["random_seed"] + 1), 0.5)
    else:
        crosswalk_matrix = None
        midpoint_matrix = None
    need_draws, need_cell_draws, prob_draws = _poststratify(
        fit, cells, cell_info, population, crosswalk_matrix, rng)

    # 上级总量校准：仅在同口径总量已知时用正乘法因子，绝不做减法摊派
    control = next((row for row in data["controls"] if row["wave"] == wave), None)
    total_need = control["total_need"] if control else None
    if total_need is not None:
        total_draw = need_draws.sum(axis=1)
        factors = np.where(total_draw > 0, total_need / np.maximum(total_draw, 1e-12), 1.0)
        need_draws = need_draws * factors[:, None]
        need_cell_draws = need_cell_draws * factors[:, None]
        calibration = float(factors.mean())
    else:
        calibration = None

    # 情景人口取首次抽样（observed 部分为确定值，缺口的随机插补差异在单元行披露）
    scenario_population = {area: float(population[0, [i for i, row in enumerate(cells)
                                                      if row["area"] == area]].sum())
                           for area in areas}
    direct_old = direct_estimates(data, wave, scenario_population)

    # 借用占比：随机截距后验中先验精度的权重 = 1 - 本地数据精度/(数据+先验)。
    # 用精度比而非方差比，避免 sigma 很小时后验/先验方差比全部塌成1。
    prior_precision = 1.0 / max(fit["sigma"] ** 2, 1e-12)
    borrowed_old = {}
    for index, area in enumerate(fit["areas"]):
        block = fit["beta"].size + 1 + index
        # 后验精度 = 数据精度 + 先验精度，借用占比即先验精度占后验精度的份额
        post_precision = 1.0 / max(fit["covariance"][block, block], 1e-12)
        borrowed_old[area] = float(min(1.0, prior_precision / max(post_precision, 1e-12)))
    for area in areas:
        borrowed_old.setdefault(area, 1.0)

    area_rows = []
    for index, area in enumerate(areas):
        draws_area = need_draws[:, index]
        point = float(draws_area.mean())
        low = _quantiles(draws_area, 0.025)
        high = _quantiles(draws_area, 0.975)
        pop_total = scenario_population[area]
        model_rate = point / pop_total if pop_total > 0 else None
        if boundary == "new":
            source_index = np.where(midpoint_matrix[:, index] > 0)[0]
            dependency = []
            borrowed = 0.0
            direct_numerator = 0.0
            direct_weight_sum = 0.0
            for source in source_index:
                source_area = areas[source]
                flow = float(midpoint_matrix[source, index])
                dependency.append({
                    "old_area": source_area, "flow_midpoint": flow,
                    "sample_n": direct_old.get(source_area, {}).get("sample_n", 0),
                    "borrowed_share": borrowed_old[source_area],
                })
                borrowed += flow * borrowed_old[source_area]
                if source_area in direct_old:
                    direct_numerator += flow * direct_old[source_area]["rate"]
                    direct_weight_sum += flow
            uncertain_flows = sorted({row["mapping"] for old, rows in groups.items() for row in rows
                                      if row["new_area"] == area and row["low"] != row["high"]})
            if direct_weight_sum > 0:
                direct_rate = direct_numerator / direct_weight_sum
                direct_view = {
                    "sample_n": None, "effective_n": None, "need_cases": None,
                    "rate": direct_rate, "se": None,
                    "count": direct_rate * pop_total, "population_basis": pop_total,
                    "basis": "旧社区直接率按流向混合" +
                            ("" if abs(direct_weight_sum - 1.0) < 1e-9 else "；部分来源无问卷，直接证据不完整"),
                }
            else:
                direct_view = None
            has_sample = direct_weight_sum > 0
        else:
            dependency = [{"old_area": area, "flow_midpoint": 1.0,
                           "sample_n": direct_old.get(area, {}).get("sample_n", 0),
                           "borrowed_share": borrowed_old[area]}]
            borrowed = borrowed_old[area]
            direct_view = direct_old.get(area)
            uncertain_flows = []
            has_sample = area in direct_old
        reliability = _reliability(has_sample, point, low, high, borrowed)
        area_rows.append({
            "area": area,
            "scenario_population": pop_total,
            "direct": direct_view,
            "model": {
                "rate": model_rate,
                "need_count": point,
                "q025": low,
                "q975": high,
                "borrowed_share": float(borrowed),
                "reliability": reliability,
                "has_direct_sample": has_sample,
                "evidence": "本地问卷直接证据+模型" if has_sample else "无本地问卷，完全依赖借用与人口结构外推",
            },
            "dependency": dependency,
            "crosswalk_uncertain_mappings": uncertain_flows,
        })

    cell_rows = []
    for index, info in enumerate(cell_info):
        cell_rows.append({
            **info,
            "model_rate_mean": float(prob_draws[:, index].mean()),
            "need_count_mean": float(need_cell_draws[:, index].mean()),
            "need_q025": _quantiles(need_cell_draws[:, index], 0.025),
            "need_q975": _quantiles(need_cell_draws[:, index], 0.975),
        })

    def marginal(field):
        levels = sorted({row[field] for row in cells})
        output = []
        for level in levels:
            idx = [i for i, row in enumerate(cells) if row[field] == level]
            level_draws = need_cell_draws[:, idx].sum(axis=1)
            pop_level = float(population[:, idx].sum())
            output.append({
                field: level, "population": pop_level,
                "need_count": float(level_draws.mean()),
                "q025": _quantiles(level_draws, 0.025),
                "q975": _quantiles(level_draws, 0.975),
            })
        return output

    total_draw = need_draws.sum(axis=1)
    result = {
        "kind": "small_area_need_estimate",
        "meta": {
            "method": "加权logistic混合模型（固定效应+旧社区随机截距）Laplace近似后，按人口交叉单元后分层，蒙特卡洛传播参数、缺口与流向不确定性",
            "fixed_terms": TERMS,
            "reliability_rules": RELIABILITY_RULES,
            "monte_carlo_draws": draws,
            "random_seed": resolved["random_seed"],
            "nonnegativity": "全部单元只做非负运算；上级校准仅使用正乘法因子，不存在减法摊派与负人数",
        },
        "selection": {"wave": wave, "boundary": boundary},
        "scenario": resolved,
        "model": {
            "fixed_effects": [{"term": term, "coef": float(coef)}
                              for term, coef in zip(TERMS, fit["beta"])],
            "fixed_prior_sd": FIXED_PRIOR_SD,
            "sigma_prior": {"distribution": "lognormal", "median": SIGMA_PRIOR_MEDIAN,
                            "sd_log": SIGMA_PRIOR_SDLOG},
            "random_effect_sd": fit["sigma"],
            "converged": fit["converged"],
            "survey_records": fit["records"],
            "areas_with_sample": fit["areas"],
            "areas_without_sample": [area for area in areas if area not in fit["area_index"]],
        },
        "areas": area_rows,
        "cells": cell_rows,
        "marginals": {"age": marginal("age"), "living": marginal("living"),
                      "mobility": marginal("mobility")},
        "total": {
            "need_count": float(total_draw.mean()),
            "q025": _quantiles(total_draw, 0.025),
            "q975": _quantiles(total_draw, 0.975),
            "population": float(population.sum()),
            "control_total_need": total_need,
            "calibration_factor": calibration,
            "basis": "等于各社区后分层人数之和" +
                     ("；已按上级同口径总量乘法校准" if calibration is not None else "；上级总量缺失，未添加凭空约束"),
        },
        "crosswalk_impact": crosswalk_corner_impact(
            data, groups, boundary, fit, cells, resolved, direct_old),
        "loco": loco_cross_validation(data, wave, groups, cells, resolved,
                                     resolved["random_seed"]) if resolved["loco"] else None,
        "audit": {
            "data_sha256": fingerprint(data),
            "scenario_fingerprint": fingerprint(resolved),
            "trace_note": "每个社区行附 dependency 来源流向与样本量；cell 行可追溯到人口单元状态与处理方式",
        },
    }
    return _native(result)
