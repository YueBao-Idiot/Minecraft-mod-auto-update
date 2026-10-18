from __future__ import annotations

import os
import shutil
import unittest
import uuid
import zipfile
from pathlib import Path

from mcmodupdater.downloader import Applier
from mcmodupdater.jarinfo import inspect_jar
from mcmodupdater.paths import OUTPUT_DIR
from mcmodupdater.planner import UpdatePlanner
from mcmodupdater.settings import AppSettings

from .fixtures import build_curseforge_fixture, build_modrinth_fixture
from .mock_api import MockApi


class ModrinthEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.test_root = OUTPUT_DIR / "_tests" / uuid.uuid4().hex[:8]
        cls.files_dir = cls.test_root / "http"
        cls.files_dir.mkdir(parents=True)
        cls.api = MockApi(cls.files_dir)
        cls.base = cls.api.start()
        cls.source = cls.test_root / "instance"
        (cls.source / "mods").mkdir(parents=True)
        cls.fixture = build_modrinth_fixture(cls.api, cls.source, cls.files_dir)
        cls.settings = AppSettings(
            instance=str(cls.source),
            loader="fabric",
            game_version="1.20.1",
            providers=["modrinth"],
            prefer_provider="auto",
            modrinth_api_base=f"{cls.base}/v2",
            copy_configs=True,
            config_scope="matched",
            full_instance=True,
            max_workers=3,
        )
        cls.plan = UpdatePlanner(cls.settings).build()
        cls.apply_result = None

    @classmethod
    def tearDownClass(cls) -> None:
        if os.environ.get("KEEP_TEST_OUTPUT"):
            return
        cls.api.stop()
        if cls.apply_result is not None:
            shutil.rmtree(cls.apply_result.output_dir, ignore_errors=True)
        shutil.rmtree(cls.test_root, ignore_errors=True)

    def mod(self, mod_id: str):
        for plan in self.plan.mods:
            if plan.mod.mod_id == mod_id:
                return plan
        self.fail(f"missing mod {mod_id}")

    def dep(self, project_id: str):
        for dep in self.plan.dependencies:
            if dep.project_id == project_id:
                return dep
        self.fail(f"missing dependency {project_id}")

    def test_01_jar_metadata(self) -> None:
        jar = inspect_jar(self.fixture["alpha_cur"])
        self.assertEqual(jar.mod_id, "alpha")
        self.assertEqual(jar.loader, "fabric")
        self.assertEqual(jar.version, "1.0.0")

    def test_02_plan_statuses(self) -> None:
        self.assertEqual(len(self.plan.mods), 7)
        self.assertEqual(self.plan.detected_loader, "fabric")
        self.assertEqual(self.plan.detected_game_version, "1.20.1")
        self.assertEqual(self.mod("alpha").status, "update")
        self.assertEqual(self.mod("alpha").target_version.version_number, "1.1.0")
        self.assertEqual(self.mod("beta-lib").status, "update")
        self.assertEqual(self.mod("fabric-api").status, "update")
        self.assertEqual(self.mod("sodium").status, "latest")
        self.assertEqual(self.mod("delta").status, "no_compatible")
        self.assertEqual(self.mod("searchmod").status, "update")
        self.assertEqual(self.mod("localonly").status, "not_found")

    def test_03_dependencies(self) -> None:
        gamma = self.dep("P_GAMMA")
        self.assertEqual(gamma.state, "missing")
        self.assertTrue(gamma.download)
        self.assertEqual(gamma.relation, "required")
        fapi = self.dep("P_FAPI")
        self.assertEqual(fapi.state, "satisfied")
        beta = self.dep("P_BETA")
        self.assertEqual(beta.state, "removable")
        self.assertIn("Alpha Core", beta.parents)
        self.assertEqual(self.plan.unresolved_dependencies, [])

    def test_04_configs(self) -> None:
        selected = {Path(item.relative_path).name for item in self.plan.selected_configs}
        self.assertEqual(selected, {"alpha-common.toml", "beta-lib.toml", "delta.toml"})
        unowned = next(item for item in self.plan.configs if item.relative_path.endswith("unowned.toml"))
        self.assertFalse(unowned.selected)

    def test_06_cli_plan_json(self) -> None:
        import contextlib
        import io
        import json

        from mcmodupdater import cli

        old_base = os.environ.get("MODRINTH_API_BASE")
        os.environ["MODRINTH_API_BASE"] = self.base + "/v2"
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                code = cli.main(
                    [
                        "plan",
                        "--instance",
                        str(self.source),
                        "--loader",
                        "fabric",
                        "--game-version",
                        "1.20.1",
                        "--provider",
                        "modrinth",
                        "--quiet",
                        "--json",
                    ]
                )
        finally:
            if old_base is None:
                os.environ.pop("MODRINTH_API_BASE", None)
            else:
                os.environ["MODRINTH_API_BASE"] = old_base
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["summary"]["total_mods"], 7)
        self.assertEqual(payload["summary"]["updates"], 4)

    def test_05_apply(self) -> None:
        source_bytes = self.fixture["alpha_cur"].read_bytes()
        source_hash = __import__("hashlib").sha512(source_bytes).hexdigest()
        applier = Applier(self.plan, self.settings)
        result = applier.apply()
        type(self).apply_result = result
        self.assertEqual(len(result.downloaded), 5, [record.to_dict() for record in result.records])
        self.assertEqual(result.failed, [])
        self.assertEqual(result.manual, [])

        self.assertTrue(result.zip_path.exists())
        self.assertGreater(result.zip_size, 0)
        output_names = {path.name for path in result.output_dir.iterdir()}
        self.assertEqual(output_names, {result.zip_path.name, "SHA256.txt"})

        with zipfile.ZipFile(result.zip_path) as archive:
            names = set(archive.namelist())
            mods = {
                Path(name).name
                for name in names
                if name.startswith("mods/") and name.lower().endswith(".jar")
            }
            self.assertEqual(
                mods,
                {
                    "alpha-1.1.0.jar",
                    "beta-lib-2.0.1.jar",
                    "fabric-api-0.91.0.jar",
                    "searchmod-0.2.0.jar",
                    "gamma-1.0.0.jar",
                    "delta-1.0.0.jar",
                    "sodium-0.5.0.jar",
                    "localonly-1.0.0.jar",
                },
            )
            configs = {
                Path(name).name
                for name in names
                if name.startswith("config/") and name.lower().endswith(".toml")
            }
            self.assertEqual(configs, {"alpha-common.toml", "beta-lib.toml", "delta.toml"})
            self.assertIn("README_如何安装.txt", names)
            self.assertIn("manifest.json", names)
            self.assertIn("reports/更新报告.md", names)
            self.assertIn("reports/mods.csv", names)
            self.assertIn("reports/dependencies.csv", names)
            self.assertIn("reports/configs.csv", names)
            self.assertIn("reports/plan.json", names)
            self.assertIn("reports/下载清单.txt", names)
            report = archive.read("reports/更新报告.md").decode("utf-8-sig")
            self.assertIn("新增前置", report)
            self.assertIn("Gamma Addon", report)
            self.assertIn("可能不再需要", report)

        self.assertTrue(result.report_paths["markdown"].exists())
        self.assertIn("Gamma Addon", result.report_paths["markdown"].read_text(encoding="utf-8-sig"))
        self.assertEqual(result.configs_copied, 3)

        self.assertEqual(
            __import__("hashlib").sha512(self.fixture["alpha_cur"].read_bytes()).hexdigest(),
            source_hash,
        )


class CurseForgeEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.test_root = OUTPUT_DIR / "_tests" / uuid.uuid4().hex[:8]
        cls.files_dir = cls.test_root / "http"
        cls.files_dir.mkdir(parents=True)
        cls.api = MockApi(cls.files_dir)
        cls.base = cls.api.start()
        cls.source = cls.test_root / "instance"
        (cls.source / "mods").mkdir(parents=True)
        cls.fixture = build_curseforge_fixture(cls.api, cls.source, cls.files_dir)
        cls.settings = AppSettings(
            instance=str(cls.source),
            loader="forge",
            game_version="1.20.1",
            providers=["curseforge"],
            curseforge_api_key="test-key",
            curseforge_api_base=f"{cls.base}/v1",
            copy_configs=True,
            full_instance=True,
            max_workers=2,
        )
        cls.plan = UpdatePlanner(cls.settings).build()

    @classmethod
    def tearDownClass(cls) -> None:
        if os.environ.get("KEEP_TEST_OUTPUT"):
            return
        cls.api.stop()
        shutil.rmtree(cls.test_root, ignore_errors=True)

    def test_cf_plan(self) -> None:
        self.assertEqual(self.plan.detected_loader, "forge")
        cfmod = self.plan.mods[0]
        self.assertEqual(cfmod.mod.mod_id, "cfmod")
        self.assertEqual(cfmod.status, "update")
        self.assertEqual(cfmod.provider_label, "CurseForge")
        self.assertEqual(cfmod.target_version.version_number, "CF Mod 1.1.0")
        cflib = next(dep for dep in self.plan.dependencies if dep.project_id == "102")
        self.assertEqual(cflib.state, "missing")
        self.assertTrue(cflib.download)
        self.assertEqual(cflib.name, "CF Library")

    def test_cf_apply(self) -> None:
        result = Applier(self.plan, self.settings).apply()
        try:
            self.assertEqual(len(result.downloaded), 2, [record.to_dict() for record in result.records])
            with zipfile.ZipFile(result.zip_path) as archive:
                names = set(archive.namelist())
                mods = {
                    Path(name).name
                    for name in names
                    if name.startswith("mods/") and name.lower().endswith(".jar")
                }
                self.assertIn("cfmod-1.1.0.jar", mods)
                self.assertIn("cflib-1.0.0.jar", mods)
                self.assertNotIn("cfmod-1.0.0.jar", mods)
                self.assertIn("config/cfmod-common.toml", names)
        finally:
            shutil.rmtree(result.output_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
