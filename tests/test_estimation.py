"""小域需求估计的统计识别、支持范围与口径回归。"""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.request import Request, urlopen
import json
import math
import unittest

from neighborhood_need.estimation import (
    estimate, validate_scenario, crosswalk_groups, fit_glmm, FIXED_PRIOR_SD,
    SIGMA_PRIOR_MEDIAN, SIGMA_PRIOR_SDLOG,
)
from neighborhood_need.service import Workbench
from neighborhood_need.server import make_server


ROOT = Path(__file__).resolve().parents[1]


class EstimationTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures" / "data.json").read_text())
        self.selection = {"wave": "autumn", "boundary": "new"}
        self.scenario = {"monte_carlo_draws": 200, "random_seed": 7}

    def test_result_is_serializable_and_finite(self):
        result = estimate(self.data, self.selection, self.scenario)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["kind"], "small_area_need_estimate")
        for row in result["areas"]:
            for key in ("need_count", "q025", "q975"):
                self.assertTrue(math.isfinite(row["model"][key]))

    def test_no_sample_area_is_never_packaged_as_direct_evidence(self):
        result = estimate(self.data, self.selection, self.scenario)
        a10 = next(row for row in result["areas"] if row["area"] == "A10")
        self.assertIsNone(a10["direct"])
        self.assertFalse(a10["model"]["has_direct_sample"])
        self.assertEqual(a10["model"]["borrowed_share"], 1.0)
        self.assertEqual(a10["model"]["reliability"], "低")
        self.assertIn("无本地问卷", a10["model"]["evidence"])
        self.assertIn("A10", result["model"]["areas_without_sample"])
        # 有样本社区的直接证据必须存在
        a05 = next(row for row in result["areas"] if row["area"] == "A05")
        self.assertIsNotNone(a05["direct"])
        self.assertTrue(a05["model"]["has_direct_sample"])

    def test_all_counts_non_negative_including_intervals(self):
        result = estimate(self.data, self.selection, self.scenario)
        for row in result["areas"]:
            self.assertGreaterEqual(row["model"]["q025"], 0.0)
            self.assertLessEqual(row["model"]["q025"], row["model"]["q975"])
        for cell in result["cells"]:
            self.assertGreaterEqual(cell["scenario_count"], 0.0)
            self.assertGreaterEqual(cell["need_q025"], 0.0)

    def test_impossible_cells_are_structural_zeros(self):
        result = estimate(self.data, self.selection, self.scenario)
        impossible = [cell for cell in result["cells"] if cell["status"] == "impossible"]
        self.assertTrue(impossible)
        for cell in impossible:
            self.assertEqual(cell["scenario_count"], 0.0)
            self.assertEqual(cell["need_count_mean"], 0.0)

    def test_unknown_cells_distinguished_from_zeros_and_disclosed(self):
        result = estimate(self.data, self.selection, self.scenario)
        unknown = [cell for cell in result["cells"] if cell["status"] == "unknown"]
        self.assertTrue(unknown)
        for cell in unknown:
            self.assertIn("缺口", cell["treatment"])
            self.assertGreater(cell["scenario_count"], 0.0)
        conservative = estimate(self.data, self.selection,
                                {**self.scenario, "unknown_policy": "conservative"})
        for cell in conservative["cells"]:
            if cell["status"] == "unknown":
                self.assertEqual(cell["scenario_count"], 0.0)
                self.assertIn("保守", cell["treatment"])

    def test_manual_unknown_policy_requires_all_cells(self):
        with self.assertRaises(ValueError):
            validate_scenario(self.data, {"unknown_policy": "manual"})
        resolved = validate_scenario(self.data, {
            "unknown_policy": "manual",
            "unknown_counts": {
                "A10|younger|alone|limited": 10,
                "A10|younger|family|limited": 10,
                "A10|older|alone|limited": 10,
                "A10|older|family|limited": 10,
            }})
        self.assertEqual(resolved["unknown_policy"], "manual")

    def test_area_total_equals_sum_of_area_estimates(self):
        result = estimate(self.data, self.selection, self.scenario)
        summed = sum(row["model"]["need_count"] for row in result["areas"])
        self.assertAlmostEqual(summed, result["total"]["need_count"], places=6)

    def test_population_scenario_multipliers_are_non_negative_and_effective(self):
        baseline = estimate(self.data, self.selection, self.scenario)
        adjusted = estimate(self.data, self.selection,
                            {**self.scenario, "population_multiplier": 1.2})
        self.assertAlmostEqual(
            adjusted["total"]["population"] / baseline["total"]["population"], 1.2, places=6)
        with self.assertRaises(ValueError):
            estimate(self.data, self.selection,
                     {**self.scenario, "population_multiplier": -0.1})

    def test_reproducible_with_same_seed(self):
        first = estimate(self.data, self.selection, self.scenario)
        second = estimate(self.data, self.selection, self.scenario)
        self.assertEqual(
            [row["model"]["need_count"] for row in first["areas"]],
            [row["model"]["need_count"] for row in second["areas"]])

    def test_crosswalk_validation_rejects_infeasible_flows(self):
        broken = deepcopy(self.data)
        broken["crosswalk"][0]["low"] = 0.95
        with self.assertRaises(ValueError):
            crosswalk_groups(broken)

    def test_crosswalk_impact_reports_uncertain_mappings_or_symmetry(self):
        result = estimate(self.data, self.selection, self.scenario)
        impact = result["crosswalk_impact"]
        self.assertTrue(impact["uncertain"])
        self.assertTrue(impact["affected_areas"] == [] or impact["affected_areas"])
        self.assertTrue("流向" in impact["note"] or "映射" in impact["note"])
        old_boundary = estimate(self.data, {"wave": "autumn", "boundary": "old"}, self.scenario)
        self.assertFalse(old_boundary["crosswalk_impact"]["uncertain"])

    def test_crosswalk_impact_is_nonzero_when_ranges_asymmetric(self):
        data = deepcopy(self.data)
        # 非对称可行角点：A00 与 A01 的区间宽度不同，角点混合比例随之变化
        data["crosswalk"] = [
            {"mapping": "0-a", "old_area": "A00", "new_area": "A00", "low": 0.7, "high": 0.95},
            {"mapping": "0-b", "old_area": "A00", "new_area": "A01", "low": 0.05, "high": 0.3},
            {"mapping": "1-a", "old_area": "A01", "new_area": "A01", "low": 0.7, "high": 0.9},
            {"mapping": "1-b", "old_area": "A01", "new_area": "A00", "low": 0.1, "high": 0.3},
        ] + [row for row in data["crosswalk"] if row["old_area"] not in {"A00", "A01"}]
        result = estimate(data, {"wave": "spring", "boundary": "new"}, self.scenario)
        impact = result["crosswalk_impact"]
        self.assertTrue(impact["affected_areas"])
        self.assertTrue(any(item["direct_rate_high"] > item["direct_rate_low"]
                            for item in impact["affected_areas"]
                            if item["direct_rate_low"] is not None))

    def test_loco_holds_out_whole_communities_and_reports_three_baselines(self):
        result = estimate(self.data, self.selection, self.scenario)
        loco = result["loco"]
        self.assertIn("完整留出一个社区", loco["held_out_kind"])
        held = {row["area"] for row in loco["areas"]}
        sampled = {row["area"] for row in self.data["surveys"]
                   if row["wave"] == self.selection["wave"]}
        self.assertEqual(held, sampled)
        self.assertEqual(set(loco["metrics"]),
                         {"mrp_model", "adjusted_poststratification", "street_average_benchmark"})
        for row in loco["areas"]:
            self.assertIsInstance(row["covered"], bool)

    def test_calibration_uses_positive_factor_and_keeps_nonnegativity(self):
        controlled = deepcopy(self.data)
        target = 1000.0
        controlled["controls"] = [
            {"wave": "spring", "total_need": None, "population_version": "new"},
            {"wave": "autumn", "total_need": target, "population_version": "new"},
        ]
        result = estimate(controlled, self.selection, self.scenario)
        self.assertIsNotNone(result["total"]["calibration_factor"])
        self.assertGreater(result["total"]["calibration_factor"], 0.0)
        self.assertAlmostEqual(result["total"]["need_count"], target, places=4)
        self.assertTrue(all(row["model"]["q025"] >= 0 for row in result["areas"]))

    def test_missing_control_does_not_invent_constraint(self):
        result = estimate(self.data, self.selection, self.scenario)
        self.assertIsNone(result["total"]["control_total_need"])
        self.assertIsNone(result["total"]["calibration_factor"])
        self.assertIn("上级总量缺失", result["total"]["basis"])

    def test_traceability_fingerprints_and_dependency_sources(self):
        result = estimate(self.data, self.selection, self.scenario)
        self.assertRegex(result["audit"]["data_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["audit"]["scenario_fingerprint"], r"^[0-9a-f]{64}$")
        for row in result["areas"]:
            self.assertTrue(row["dependency"])
            self.assertAlmostEqual(
                sum(item["flow_midpoint"] for item in row["dependency"]), 1.0, places=6)

    def test_priors_are_disclosed_in_model_metadata(self):
        result = estimate(self.data, self.selection, self.scenario)
        model = result["model"]
        self.assertEqual(model["fixed_prior_sd"], FIXED_PRIOR_SD)
        self.assertEqual(model["sigma_prior"]["median"], SIGMA_PRIOR_MEDIAN)
        self.assertEqual(model["sigma_prior"]["sd_log"], SIGMA_PRIOR_SDLOG)
        self.assertTrue(model["converged"])

    def test_estimation_does_not_mutate_inputs(self):
        before = deepcopy(self.data)
        estimate(self.data, self.selection, self.scenario)
        self.assertEqual(before, self.data)

    def test_invalid_selection_rejected(self):
        with self.assertRaises(ValueError):
            estimate(self.data, {"wave": "nope", "boundary": "new"}, self.scenario)

    def test_fit_handles_quasi_separated_cell_without_divergence(self):
        records = [row for row in self.data["surveys"] if row["wave"] == "autumn"]
        fit = fit_glmm(records)
        self.assertTrue(fit["converged"])
        self.assertTrue(all(math.isfinite(value) for value in fit["beta"]))
        self.assertGreater(fit["sigma"], 0.0)


class ServiceAndHttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.server = make_server(ROOT, self.temporary.name, port=0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def post(self, path, body):
        request = Request(self.base + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request) as response:
                return response.status, json.load(response)
        except Exception as error:
            return error.code, json.load(error)

    def test_estimate_endpoint_and_frozen_notebook(self):
        status, result = self.post("/api/estimate",
                                   {"selection": {"wave": "spring", "boundary": "new"},
                                    "scenario": {"monte_carlo_draws": 100}})
        self.assertEqual(status, 200)
        self.assertEqual(result["view"]["kind"], "small_area_need_estimate")
        status, saved = self.post("/api/notebooks",
                                  {"title": "估计存档", "kind": "estimate",
                                   "selection": {"wave": "spring", "boundary": "new"},
                                   "scenario": {"monte_carlo_draws": 100}})
        self.assertEqual(status, 201)
        with urlopen(self.base + "/api/notebooks/" + str(saved["id"])) as response:
            reopened = json.load(response)
        self.assertEqual(reopened["result"]["view"]["areas"], saved["result"]["view"]["areas"])

    def test_estimate_rejects_bad_scenario_over_http(self):
        status, payload = self.post("/api/estimate", {"scenario": {"monte_carlo_draws": 10}})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)

    def test_existing_explore_behavior_unchanged(self):
        status, result = self.post("/api/explore",
                                   {"selection": {"wave": "autumn", "boundary": "old"}})
        self.assertEqual(status, 200)
        self.assertIn("rows", result["view"])
        self.assertIn("不是新边界需求估计", result["view"]["notes"][0])


if __name__ == "__main__":
    unittest.main()
