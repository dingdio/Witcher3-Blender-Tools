"""Pure-Python regression tests for the cloth APX sanitizer."""

from __future__ import annotations

import importlib.util
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "witcher3_tools"
CLOTH_ROOT = PACKAGE_ROOT / "cloth"
APX_PATH = CLOTH_ROOT / "apx.py"


def _load_apx_module():
    """Load cloth.apx without importing the Blender-dependent add-on root."""
    package_name = "_witcher_cloth_apx_test"
    cloth_package_name = f"{package_name}.cloth"
    extension_paths_name = f"{package_name}.extension_paths"
    module_name = f"{cloth_package_name}.apx"

    package = types.ModuleType(package_name)
    package.__path__ = [str(PACKAGE_ROOT)]
    package.__package__ = package_name
    cloth_package = types.ModuleType(cloth_package_name)
    cloth_package.__path__ = [str(CLOTH_ROOT)]
    cloth_package.__package__ = cloth_package_name

    package_modules = {
        package_name: package,
        cloth_package_name: cloth_package,
    }
    missing = object()
    managed_names = (*package_modules, extension_paths_name, module_name)
    saved_modules = {
        name: sys.modules.get(name, missing)
        for name in managed_names
    }

    try:
        sys.modules.update(package_modules)

        extension_spec = importlib.util.spec_from_file_location(
            extension_paths_name,
            PACKAGE_ROOT / "extension_paths.py",
        )
        extension_module = importlib.util.module_from_spec(extension_spec)
        sys.modules[extension_paths_name] = extension_module
        extension_spec.loader.exec_module(extension_module)

        spec = importlib.util.spec_from_file_location(module_name, APX_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, previous in saved_modules.items():
            if previous is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


apx = _load_apx_module()


def _clothing_apx_xml(*, valid: bool) -> str:
    graph_indices = "0 1 2" if valid else "0 1 2 0 0 1 0 1 4 0 2"
    graph_index_size = "3" if valid else "11"
    physical_indices = "0 1 2" if valid else "0 1 2 1 1 2 0 2 9"
    physical_index_size = "3" if valid else "9"
    physical_num_indices = "3" if valid else "9"
    array_size = "1" if valid else "2"
    extra_entry = "" if valid else '<value type="Ref"><struct name="" /></value>'
    material_size = "1" if valid else "99"

    return f"""<?xml version="1.0" encoding="utf-8"?>
<NxParameters numObjects="1" version="1.0">
  <value name="" type="Ref" className="ClothingAssetParameters">
    <struct name="">
      <array name="physicalMeshes" size="{array_size}" type="Ref">
        <value type="Ref" className="ClothingPhysicalMeshParameters">
          <struct name="">
            <struct name="physicalMesh">
              <value name="numVertices" type="U32">4</value>
              <value name="numIndices" type="U32">{physical_num_indices}</value>
              <array name="indices" size="{physical_index_size}" type="U32">{physical_indices}</array>
            </struct>
          </struct>
        </value>
        {extra_entry}
      </array>
      <array name="graphicalLods" size="{array_size}" type="Ref">
        <value type="Ref" className="ClothingGraphicalLodParameters">
          <struct name="">
            <value name="renderMeshAsset" type="Ref">
              <struct name="">
                <array name="submeshes" size="1" type="Ref">
                  <value type="Ref" className="SubmeshParameters">
                    <struct name="">
                      <value name="vertexBuffer" type="Ref">
                        <struct name="">
                          <value name="vertexCount" type="U32">4</value>
                        </struct>
                      </value>
                      <array name="indexBuffer" size="{graph_index_size}" type="U32">{graph_indices}</array>
                    </struct>
                  </value>
                </array>
              </struct>
            </value>
          </struct>
        </value>
        {extra_entry}
      </array>
      <value name="materialLibrary" type="Ref">
        <struct name="">
          <array name="materials" size="{material_size}" type="Struct">
            <struct><value name="materialName" type="String">test</value></struct>
          </array>
        </struct>
      </value>
    </struct>
  </value>
</NxParameters>
"""


def _destructible_apx_xml() -> str:
    return """<?xml version="1.0" encoding="utf-8"?>
<NxParameters numObjects="1" version="1.0">
  <value name="" type="Ref" className="DestructibleAssetParameters">
    <struct name="">
      <value name="renderMeshAsset" type="Ref">
        <struct name="">
          <array name="submeshes" size="1" type="Ref">
            <value type="Ref" className="SubmeshParameters">
              <struct name="">
                <value name="vertexBuffer" type="Ref">
                  <struct name="">
                    <value name="vertexCount" type="U32">4</value>
                    <value name="vertexFormat" type="Ref">
                      <struct name="">
                        <array name="bufferFormats" size="1" type="Struct">
                          <struct><value name="name" type="String">SEMANTIC_POSITION</value></struct>
                        </array>
                      </struct>
                    </value>
                    <array name="buffers" size="1" type="Ref">
                      <value type="Ref">
                        <struct name="">
                          <array name="data" size="12" type="F32">0 0 0 1 0 0 2 0 0 0 1 0</array>
                        </struct>
                      </value>
                    </array>
                  </struct>
                </value>
                <array name="indexBuffer" size="6" type="U32">0 1 2 0 1 3</array>
              </struct>
            </value>
          </array>
        </struct>
      </value>
    </struct>
  </value>
</NxParameters>
"""


class TestApxArrayParsing(unittest.TestCase):
    def test_integer_and_float_arrays_accept_commas_and_whitespace(self):
        self.assertEqual(
            apx._parse_apx_int_array_text("0, 1\n2\t3"),
            [0, 1, 2, 3],
        )
        self.assertEqual(
            apx._parse_apx_float_array_text("1.5, -2\n1e-3"),
            [1.5, -2.0, 0.001],
        )
        self.assertEqual(
            apx._format_apx_int_array_text([0, 1, 2, 3]),
            "0 1 2 3",
        )

    def test_triangle_filter_reports_each_failure_class(self):
        indices = [
            0, 1, 2,  # zero-area
            0, 0, 1,  # repeated index
            0, 1, 4,  # outside the vertex array
            0, 2, 3,  # valid
            0,        # truncated tail
        ]
        positions = [
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (2.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        ]

        filtered, stats = apx._sanitize_apx_triangle_indices(
            indices,
            vertex_count=4,
            positions=positions,
        )

        self.assertEqual(filtered, [0, 2, 3])
        self.assertEqual(stats["removed_total"], 4)
        self.assertEqual(stats["removed_degenerate"], 1)
        self.assertEqual(stats["removed_zero_area"], 1)
        self.assertEqual(stats["removed_out_of_range"], 1)
        self.assertEqual(stats["removed_truncated"], 1)

    def test_triangle_array_normalizes_stale_size_without_reformatting(self):
        array_elem = ElementTree.Element("array", size="99")
        array_elem.text = "0, 1\n2"

        stats = apx._sanitize_apx_triangle_array(array_elem, vertex_count=3)

        self.assertEqual(stats["removed_total"], 0)
        self.assertEqual(stats["triangle_count"], 1)
        self.assertEqual(array_elem.attrib["size"], "3")
        self.assertEqual(array_elem.text, "0, 1\n2")


class TestApxFileSanitization(unittest.TestCase):
    def test_clothing_copy_is_sanitized_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "broken_clothing.apx"
            source.write_text(_clothing_apx_xml(valid=False), encoding="utf-8")
            source_bytes = source.read_bytes()

            with mock.patch.object(
                apx,
                "get_temp_root",
                side_effect=lambda create=False: str(temp_path),
            ):
                output = Path(apx.sanitize_apx_for_import(str(source)))
                repeated_output = Path(apx.sanitize_apx_for_import(str(source)))

            self.assertNotEqual(output, source)
            self.assertEqual(output, repeated_output)
            self.assertEqual(output.parent, temp_path / "sanitized_apx")
            self.assertRegex(
                output.name,
                re.compile(r"^broken_clothing\.[0-9a-f]{12}\.apx$"),
            )
            self.assertEqual(source.read_bytes(), source_bytes)

            root = ElementTree.parse(output).getroot()
            graphical_lods = root.find(".//array[@name='graphicalLods']")
            physical_meshes = root.find(".//array[@name='physicalMeshes']")
            materials = root.find(".//array[@name='materials']")
            graph_indices = root.find(".//array[@name='indexBuffer']")
            physical_indices = root.find(".//array[@name='indices']")
            physical_num_indices = root.find(".//value[@name='numIndices']")

            self.assertEqual(len(graphical_lods), 1)
            self.assertEqual(graphical_lods.attrib["size"], "1")
            self.assertEqual(len(physical_meshes), 1)
            self.assertEqual(physical_meshes.attrib["size"], "1")
            self.assertEqual(materials.attrib["size"], "1")
            self.assertEqual(graph_indices.text, "0 1 2")
            self.assertEqual(graph_indices.attrib["size"], "3")
            self.assertEqual(physical_indices.text, "0 1 2")
            self.assertEqual(physical_indices.attrib["size"], "3")
            self.assertEqual(physical_num_indices.text, "3")

    def test_valid_clothing_apx_is_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "valid_clothing.apx"
            source.write_text(_clothing_apx_xml(valid=True), encoding="utf-8")

            output = apx.sanitize_apx_for_import(str(source))

            self.assertEqual(output, str(source))

    def test_destructible_copy_removes_zero_area_triangle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "broken_destructible.apx"
            source.write_text(_destructible_apx_xml(), encoding="utf-8")

            with mock.patch.object(
                apx,
                "get_temp_root",
                side_effect=lambda create=False: str(temp_path),
            ):
                output = Path(apx.sanitize_apx_for_import(str(source)))

            self.assertNotEqual(output, source)
            index_buffer = ElementTree.parse(output).getroot().find(
                ".//array[@name='indexBuffer']"
            )
            self.assertEqual(index_buffer.text, "0 1 3")
            self.assertEqual(index_buffer.attrib["size"], "3")

    def test_unsupported_missing_and_malformed_inputs_are_unchanged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            apb = temp_path / "cloth.apb"
            apb.write_text("not APX XML", encoding="utf-8")
            malformed = temp_path / "malformed.apx"
            malformed.write_text("<NxParameters>", encoding="utf-8")
            missing = temp_path / "missing.apx"

            self.assertEqual(
                apx.sanitize_apx_for_import(str(apb)),
                str(apb),
            )
            self.assertEqual(
                apx.sanitize_apx_for_import(str(malformed)),
                str(malformed),
            )
            self.assertEqual(
                apx.sanitize_apx_for_import(str(missing)),
                str(missing),
            )


if __name__ == "__main__":
    unittest.main()
