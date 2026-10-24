from __future__ import annotations

import json
import shutil
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from mcmodupdater.downloader import build_package_name
from mcmodupdater.instance import detect_instance
from mcmodupdater.jarinfo import inspect_jar
from mcmodupdater.models import ModFile, ModPlan, UpdatePlan
from mcmodupdater.paths import OUTPUT_DIR
from mcmodupdater.planner import canonical_version, versions_equivalent
from mcmodupdater.providers.modrinth import ModrinthProvider
from mcmodupdater.util import extract_mc_versions, is_mc_version, is_plausible_mc_version

from .fixtures import make_forge_jar, make_neoforge_jar


class LoaderMetadataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = OUTPUT_DIR / "_tests" / uuid.uuid4().hex[:8]
        cls.root.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_neoforge_metadata(self) -> None:
        jar = inspect_jar(make_neoforge_jar(self.root / "neomod.jar", mod_id="neomod", name="Neo Mod", version="1.2.3"))
        self.assertEqual(jar.mod_id, "neomod")
        self.assertEqual(jar.loader, "neoforge")
        self.assertEqual(jar.version, "1.2.3")

    def test_forge_metadata(self) -> None:
        jar = inspect_jar(
            make_forge_jar(self.root / "forgemod.jar", mod_id="forgemod", name="Forge Mod", version="2.0.0")
        )
        self.assertEqual(jar.mod_id, "forgemod")
        self.assertEqual(jar.loader, "forge")
        self.assertFalse(any(dep.mod_id in ("minecraft", "forge") for dep in jar.dependencies))

    def test_version_isolation_folder_detection(self) -> None:
        profile = self.root / ".minecraft" / "versions" / "1.20.1-forge"
        (profile / "mods").mkdir(parents=True)
        make_forge_jar(profile / "mods" / "demo.jar", mod_id="demo", name="Demo", version="1.0.0")
        (profile / "1.20.1-forge.json").write_text(
            json.dumps({"id": "1.20.1-forge", "inheritsFrom": "1.20.1"}),
            encoding="utf-8",
        )
        detected = detect_instance(profile)
        self.assertEqual(detected.loader, "forge")
        self.assertEqual(detected.game_version, "1.20.1")
        self.assertEqual(detected.mods_dir, profile / "mods")


class PackageNameTest(unittest.TestCase):
    def test_name_contains_source_target_loader_and_time(self) -> None:
        plan = UpdatePlan(
            source_root=Path("instance"),
            mods_dir=Path("instance/mods"),
            loader="fabric",
            game_version="1.21.1",
            providers=["modrinth"],
            detected_loader="fabric",
            detected_game_version="1.20.1",
        )
        name = build_package_name(plan, datetime(2026, 10, 18, 15, 30))
        self.assertEqual(name, "ModPack_1.20.1_to_1.21.1_Fabric_20261018-1530")

    def test_name_marks_cross_loader(self) -> None:
        plan = UpdatePlan(
            source_root=Path("instance"),
            mods_dir=Path("instance/mods"),
            loader="neoforge",
            game_version="1.21.1",
            providers=["modrinth"],
            detected_loader="forge",
            detected_game_version="1.21.1",
        )
        name = build_package_name(plan, datetime(2026, 10, 18, 15, 30))
        self.assertIn("from_Forge", name)
        self.assertIn("NeoForge", name)


class VersionRulesTest(unittest.TestCase):
    def test_modern_minecraft_versions(self) -> None:
        self.assertTrue(is_mc_version("1.20.1"))
        self.assertTrue(is_mc_version("26.3"))
        self.assertTrue(is_mc_version("26.1.2"))
        self.assertFalse(is_mc_version("26.4-snapshot-4"))
        self.assertTrue(is_mc_version("1.0.0"))  # 语法上是版本号，但是否像 MC 版本由 plausible 判断

    def test_plausible_versions(self) -> None:
        self.assertTrue(is_plausible_mc_version("26.3"))
        self.assertTrue(is_plausible_mc_version("1.20.1"))
        self.assertFalse(is_plausible_mc_version("1.0.0"))

    def test_canonical_versions(self) -> None:
        self.assertEqual(canonical_version("mc1.20.1-0.5.11"), "0.5.11")
        self.assertEqual(canonical_version("mc26.3-0.5.13-fabric"), "0.5.13")
        self.assertTrue(versions_equivalent("0.5.13", "mc26.3-0.5.13-fabric"))
        self.assertTrue(versions_equivalent("31.15.0.82", "31.15.0.82 for Fabric 26.3"))
        self.assertFalse(versions_equivalent("0.5.11", "0.5.13"))

    def test_prerelease_versions_are_not_equivalent(self) -> None:
        self.assertFalse(versions_equivalent("2.3.0-beta.10+26.1", "2.3.0-beta.9+1.20.2"))
        self.assertTrue(versions_equivalent("2.3.0-beta.10+26.1", "2.3.0-beta.10+1.20.2"))

    def test_local_incompatible_mod_is_detected(self) -> None:
        incompatible = ModFile(
            path=Path("old.jar"),
            filename="old.jar",
            size=0,
            mc_versions=["26.1"],
            loaders=["fabric"],
            loader="fabric",
        )
        self.assertFalse(ModPlan(mod=incompatible).supports_target("1.20.2", "fabric"))
        compatible = ModFile(
            path=Path("new.jar"),
            filename="new.jar",
            size=0,
            mc_versions=["1.20.2"],
            loaders=["fabric"],
            loader="fabric",
        )
        self.assertTrue(ModPlan(mod=compatible).supports_target("1.20.2", "fabric"))

    def test_name_search_requires_exact_match(self) -> None:
        mod = ModFile(
            path=Path("rtweaks.jar"),
            filename="rtweaks.jar",
            size=0,
            mod_ids=["rtweaks"],
            name="R-Tweaks",
        )
        self.assertEqual(
            ModrinthProvider._score_candidate(mod, {"slug": "villagertweaks", "title": "Villager Tweaks"}),
            0,
        )
        self.assertEqual(
            ModrinthProvider._score_candidate(mod, {"slug": "rtweaks", "title": "R-Tweaks"}),
            100,
        )

    def test_extract_versions(self) -> None:
        self.assertEqual(extract_mc_versions("mc26.3-fabric-31.15.0.82"), ["26.3"])
        self.assertEqual(extract_mc_versions("sodium-fabric-0.5.11+mc1.20.1.jar"), ["1.20.1"])


if __name__ == "__main__":
    unittest.main()
