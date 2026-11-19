"""Lab safety checks use mocked Docker only; these are not integration tests."""

import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "connection_guard_lab", Path(__file__).resolve().parents[1] / "scripts" / "connection_guard_lab.py",
)
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)
NAME = lab.PREFIX + "123456789abc"
IMAGE_ID = "sha256:" + "a" * 64


def resources():
    labels = {lab.LABEL: "1", lab.NAME_LABEL: NAME}
    container = {
        "Name": "/" + NAME, "Id": "b" * 64, "Config": {"Labels": labels},
        "Mounts": [{"Type": "volume", "Name": NAME + "-data", "Destination": "/data"}],
        "HostConfig": {"PortBindings": {
            "5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}],
            "25565/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}],
        }},
    }
    volume = {"Name": NAME + "-data", "Labels": labels, "CreatedAt": "2026-01-01T00:00:00Z"}
    return container, volume


class LabSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stdout = patch("sys.stdout", new_callable=io.StringIO)
        self.stderr = patch("sys.stderr", new_callable=io.StringIO)
        self.stdout.start()
        self.stderr.start()
        self.addCleanup(self.stdout.stop)
        self.addCleanup(self.stderr.stop)

    def test_rejects_broad_or_production_target_before_docker(self):
        for name in ["byte-panel", "/", "..", NAME + "/data", lab.PREFIX + "*"]:
            with patch.object(lab, "verify_daemon") as daemon:
                self.assertEqual(lab.main(["cleanup", name, "--confirm", name]), 1)
                daemon.assert_not_called()

    def test_mismatched_confirmation_never_contacts_docker(self):
        with patch.object(lab, "verify_daemon") as daemon:
            self.assertEqual(lab.main(["cleanup", NAME, "--confirm", "yes"]), 1)
            daemon.assert_not_called()

    def test_resource_labels_mounts_and_ports_are_mandatory(self):
        container, volume = resources()
        cases = []
        bad = copy.deepcopy(container)
        bad["Config"]["Labels"] = {}
        cases.append((bad, volume))
        bad = copy.deepcopy(volume)
        bad["Labels"] = {}
        cases.append((container, bad))
        bad = copy.deepcopy(container)
        bad["Mounts"].append({"Type": "bind", "Source": "/production", "Destination": "/prod"})
        cases.append((bad, volume))
        bad = copy.deepcopy(container)
        bad["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostIp"] = "0.0.0.0"
        cases.append((bad, volume))
        bad = copy.deepcopy(container)
        bad["HostConfig"]["PortBindings"]["5051/tcp"] = [{"HostIp": "127.0.0.1"}]
        cases.append((bad, volume))
        cases.append((container, None))
        for candidate, disk in cases:
            with self.subTest(candidate=candidate, disk=disk), self.assertRaises(lab.LabError):
                lab.verify_resources(NAME, candidate, disk)

    def test_valid_lab_resource_pair_is_accepted(self):
        lab.verify_resources(NAME, *resources())

    def test_cleanup_removes_inspected_id_and_exact_labelled_volume_only(self):
        container, volume = resources()
        with patch.object(lab, "find_container", return_value=container), \
                patch.object(lab, "find_volume", return_value=volume), \
                patch.object(lab, "docker", return_value="") as docker:
            lab.cleanup(NAME, NAME)
            self.assertEqual(docker.call_args_list, [
                unittest.mock.call("container", "rm", "--force", container["Id"]),
                unittest.mock.call("volume", "rm", NAME + "-data"),
            ])

    def test_failed_start_volume_only_can_be_cleaned(self):
        _, volume = resources()
        with patch.object(lab, "find_container", return_value=None), \
                patch.object(lab, "find_volume", return_value=volume), \
                patch.object(lab, "docker", return_value="") as docker:
            lab.cleanup(NAME, NAME)
            docker.assert_called_once_with("volume", "rm", NAME + "-data")

    def test_changed_volume_is_not_deleted(self):
        container, volume = resources()
        replacement = dict(volume, CreatedAt="2026-02-01T00:00:00Z")
        with patch.object(lab, "find_container", return_value=container), \
                patch.object(lab, "find_volume", side_effect=[volume, replacement]), \
                patch.object(lab, "docker", return_value="") as docker:
            with self.assertRaises(lab.LabError):
                lab.cleanup(NAME, NAME)
            docker.assert_called_once_with("container", "rm", "--force", container["Id"])

    def test_unsafe_volume_prevents_even_container_deletion(self):
        container, volume = resources()
        volume["Labels"] = {}
        with patch.object(lab, "find_container", return_value=container), \
                patch.object(lab, "find_volume", return_value=volume), \
                patch.object(lab, "docker") as docker:
            with self.assertRaises(lab.LabError):
                lab.cleanup(NAME, NAME)
            docker.assert_not_called()

    def test_start_pins_local_image_and_never_exposes_bridge_or_mounts_host(self):
        container, volume = resources()
        def response(*args):
            if args[:2] == ("image", "inspect"):
                return json.dumps([{"Id": IMAGE_ID, "Os": "linux"}])
            return ""
        with patch.object(lab.uuid, "uuid4") as identifier, \
                patch.object(lab, "find_container", side_effect=[None, container]), \
                patch.object(lab, "find_volume", side_effect=[None, volume]), \
                patch.object(lab, "docker", side_effect=response) as docker:
            identifier.return_value.hex = "123456789abc" + "0" * 20
            lab.start("candidate:local")
            commands = [entry.args for entry in docker.call_args_list]
            run = next(command for command in commands if command[0] == "run")
            self.assertEqual(run[-1], IMAGE_ID)
            self.assertIn("--pull=never", run)
            self.assertIn("127.0.0.1::5000/tcp", run)
            self.assertIn("127.0.0.1::25565/tcp", run)
            self.assertIn(f"type=volume,source={NAME}-data,target=/data", run)
            self.assertFalse(any("5051" in value or "type=bind" in value for value in run))
            self.assertFalse(any(command[0] in {"pull", "build"} for command in commands))

    def test_absent_local_image_cannot_create_any_resources(self):
        with patch.object(lab, "docker", side_effect=lab.LabError("missing image")) as docker:
            with self.assertRaises(lab.LabError):
                lab.start("absent:local")
            docker.assert_called_once_with("image", "inspect", "absent:local")


if __name__ == "__main__":
    unittest.main()
