# Copyright (c) EdgeOS Contributors.
# SPDX-License-Identifier: MPL-2.0

"""CPU topology regression tests for the EdgeOS VMM."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edgeos_vm import VmError, apply_cpu_topology_overrides, qemu_smp_value


class CpuTopologyTests(unittest.TestCase):
    def test_cpu_count_replaces_inherited_template_topology(self) -> None:
        config = {
            "cpus": 4,
            "cpu_sockets": 1,
            "cpu_cores": 4,
            "cpu_threads": 1,
        }

        apply_cpu_topology_overrides(
            config, cpus=2, sockets=None, cores=None, threads=None
        )

        self.assertEqual(config["cpus"], 2)
        self.assertEqual(config["cpu_sockets"], 1)
        self.assertEqual(config["cpu_cores"], 2)
        self.assertEqual(config["cpu_threads"], 1)
        self.assertEqual(
            qemu_smp_value(config), "cpus=2,sockets=1,cores=2,threads=1"
        )

    def test_cpu_count_infers_cores_from_sockets_and_threads(self) -> None:
        config: dict[str, int] = {}

        apply_cpu_topology_overrides(
            config, cpus=8, sockets=2, cores=None, threads=2
        )

        self.assertEqual(config["cpu_cores"], 2)
        self.assertEqual(
            qemu_smp_value(config), "cpus=8,sockets=2,cores=2,threads=2"
        )

    def test_contradictory_explicit_topology_is_rejected(self) -> None:
        with self.assertRaisesRegex(VmError, "does not match"):
            apply_cpu_topology_overrides(
                {}, cpus=2, sockets=1, cores=4, threads=1
            )

    def test_invalid_persisted_topology_is_rejected_before_qemu(self) -> None:
        with self.assertRaisesRegex(VmError, "invalid CPU topology"):
            qemu_smp_value(
                {
                    "cpus": 2,
                    "cpu_sockets": 1,
                    "cpu_cores": 4,
                    "cpu_threads": 1,
                }
            )


if __name__ == "__main__":
    unittest.main()
