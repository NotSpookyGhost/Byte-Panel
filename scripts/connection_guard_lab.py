#!/usr/bin/env python3
"""Explicit-run disposable Linux Docker lab; never mounts existing server data."""

import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid


PREFIX = "byte-panel-guard-lab-"
LABEL = "app.bytedev.guard-lab"
NAME_LABEL = "app.bytedev.guard-lab-name"
NAME_RE = re.compile(r"byte-panel-guard-lab-[0-9a-f]{12}\Z")


class LabError(Exception):
    pass


def docker(*args):
    try:
        result = subprocess.run(
            ["docker", *args], check=False, text=True, capture_output=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LabError(f"Docker command could not complete: {exc}") from None
    if result.returncode:
        # No credentials are accepted on this launcher's command line.
        detail = result.stderr.strip() or result.stdout.strip() or "no details"
        raise LabError(f"Docker {' '.join(args[:2])} failed: {detail}")
    return result.stdout.strip()


def verify_daemon():
    if not shutil.which("docker"):
        raise LabError("Docker CLI is missing. Install/configure Docker separately.")
    if docker("info", "--format", "{{.OSType}}") != "linux":
        raise LabError("This harness requires a Linux Docker daemon.")


def checked_name(name):
    if not NAME_RE.fullmatch(name):
        raise LabError("Use the exact generated lab name, not a production name or path.")
    return name


def inspect_json(kind, identifier):
    try:
        data = json.loads(docker(kind, "inspect", identifier))
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            return data[0]
    except (ValueError, TypeError):
        pass
    raise LabError(f"Invalid Docker {kind} inspection response.")


def find_container(name):
    ids = docker(
        "container", "ls", "--all", "--filter", f"name=^/{name}$",
        "--format", "{{.ID}}",
    ).splitlines()
    if not ids:
        return None
    if len(ids) != 1:
        raise LabError("Ambiguous lab container; refusing to continue.")
    return inspect_json("container", ids[0])


def find_volume(name):
    volume_name = name + "-data"
    matches = docker(
        "volume", "ls", "--filter", f"name=^{volume_name}$", "--format", "{{.Name}}",
    ).splitlines()
    if not matches:
        return None
    if matches != [volume_name]:
        raise LabError("Ambiguous lab volume; refusing to continue.")
    return inspect_json("volume", volume_name)


def verify_labels(labels, name):
    if not isinstance(labels, dict) or labels.get(LABEL) != "1" or labels.get(NAME_LABEL) != name:
        raise LabError("Resource lacks the exact lab labels; refusing to touch it.")


def verify_resources(name, container, volume):
    """Validate all targets before any destructive action."""
    checked_name(name)
    volume_name = name + "-data"
    if volume is not None:
        if volume.get("Name") != volume_name:
            raise LabError("Unexpected volume name.")
        verify_labels(volume.get("Labels"), name)
    if container is not None:
        if container.get("Name") != "/" + name:
            raise LabError("Unexpected container name.")
        if not re.fullmatch(r"[0-9a-f]{64}", container.get("Id", "")):
            raise LabError("Invalid container ID.")
        verify_labels(container.get("Config", {}).get("Labels"), name)
        mounts = container.get("Mounts", [])
        if (len(mounts) != 1 or mounts[0].get("Type") != "volume"
                or mounts[0].get("Name") != volume_name
                or mounts[0].get("Destination") != "/data"):
            raise LabError("Lab mounts changed or include unrelated data; cleanup refused.")
        if volume is None:
            raise LabError("Expected labelled data volume is missing; cleanup refused.")
        bindings = container.get("HostConfig", {}).get("PortBindings") or {}
        if set(bindings) != {"5000/tcp", "25565/tcp"}:
            raise LabError("Lab port configuration changed; cleanup refused.")
        for entries in bindings.values():
            if not entries or any(entry.get("HostIp") != "127.0.0.1" for entry in entries):
                raise LabError("Lab is no longer loopback-only; cleanup refused.")


def report(name, container, volume):
    print(f"Lab: {name}")
    print(f"Volume: {volume['Name'] if volume else '(not present)'}")
    if container:
        print(f"Container ID: {container['Id']}")
        print(f"Image ID: {container.get('Image', '(unknown)')}")
        print(f"State: {container.get('State', {}).get('Status', '(unknown)')}")
        ports = container.get("NetworkSettings", {}).get("Ports") or {}
        for port, label in (("5000/tcp", "Panel"), ("25565/tcp", "Minecraft")):
            for binding in ports.get(port) or []:
                host_port = binding.get("HostPort", "?")
                address = f"127.0.0.1:{host_port}"
                print(f"{label}: {'http://' if port == '5000/tcp' else ''}{address}")
    print("These addresses belong to the Docker daemon host, not a remote CLI client.")
    print("Use disposable accounts/worlds only. No runtime checks have run yet.")
    print("Checklist: docs/connection-guard.md")
    print(f"Cleanup: python3 scripts/connection_guard_lab.py cleanup {name} --confirm {name}")


def start(image):
    if not image or image.startswith("-") or any(c.isspace() for c in image):
        raise LabError("Supply one existing local image name or image ID.")
    image_info = inspect_json("image", image)
    image_id = image_info.get("Id", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise LabError("Local image has no valid immutable ID.")
    if image_info.get("Os") != "linux":
        raise LabError("The selected image is not a Linux image.")
    # Pin the inspected image ID so retagging cannot change what is launched.
    name = PREFIX + uuid.uuid4().hex[:12]
    if find_container(name) or find_volume(name):
        raise LabError("Unexpected name collision; rerun to generate a new lab name.")
    volume_name = name + "-data"
    print(f"Creating disposable lab {name} from {image_id}", flush=True)
    try:
        docker(
            "volume", "create", "--label", f"{LABEL}=1", "--label",
            f"{NAME_LABEL}={name}", volume_name,
        )
        docker(
            "run", "--pull=never", "--restart=no", "--detach", "--name", name,
            "--label", f"{LABEL}=1", "--label", f"{NAME_LABEL}={name}",
            "--mount", f"type=volume,source={volume_name},target=/data",
            "--publish", "127.0.0.1::5000/tcp",
            "--publish", "127.0.0.1::25565/tcp",
            "--memory", "4g", "--cpus", "2", image_id,
        )
        container, volume = find_container(name), find_volume(name)
        if container is None:
            raise LabError("Docker did not return the created container.")
        verify_resources(name, container, volume)
        report(name, container, volume)
        print("During panel setup, choose modest server RAM (for example 2 GB).")
    except LabError:
        print(f"Partial lab resources may remain under exact name: {name}", file=sys.stderr)
        print(
            f"Inspect/clean only this lab: python3 scripts/connection_guard_lab.py "
            f"cleanup {name} --confirm {name}", file=sys.stderr,
        )
        raise


def status(name):
    checked_name(name)
    container, volume = find_container(name), find_volume(name)
    verify_resources(name, container, volume)
    if container is None and volume is None:
        raise LabError("No resources exist for that lab name.")
    report(name, container, volume)


def cleanup(name, confirmation):
    checked_name(name)
    if confirmation != name:
        raise LabError("Cleanup requires --confirm followed by the exact lab name.")
    container, volume = find_container(name), find_volume(name)
    verify_resources(name, container, volume)
    if container is None and volume is None:
        raise LabError("No resources exist for that lab name.")
    if container is not None:
        # Remove the inspected ID, never an unresolved user-supplied name.
        docker("container", "rm", "--force", container["Id"])
    if volume is not None:
        # Re-check after container deletion; never remove an unlabelled/replaced volume.
        current = find_volume(name)
        if current is None or current.get("CreatedAt") != volume.get("CreatedAt"):
            raise LabError("Volume changed during cleanup; remaining volume was not removed.")
        verify_resources(name, None, current)
        docker("volume", "rm", current["Name"])
    print(f"Removed only disposable lab {name} and its data volume.")
    print("The lab world, accounts and credentials are permanently deleted.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Explicit disposable Byte Guard lab; no pulls or production mounts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("start", help="Launch an existing local image in a fresh lab.")
    create.add_argument("--image", required=True, help="Existing local Linux candidate image.")
    show = sub.add_parser("status", help="Read a labelled lab's state and loopback ports.")
    show.add_argument("name")
    remove = sub.add_parser("cleanup", help="Permanently delete one verified disposable lab.")
    remove.add_argument("name")
    remove.add_argument("--confirm", required=True, help="Repeat the exact lab name.")
    args = parser.parse_args(argv)
    try:
        # Validate caller-controlled targets before contacting a Docker daemon.
        if args.command != "start":
            checked_name(args.name)
        if args.command == "cleanup" and args.confirm != args.name:
            raise LabError("Cleanup confirmation does not match the lab name.")
        verify_daemon()
        if args.command == "start":
            start(args.image)
        elif args.command == "status":
            status(args.name)
        else:
            cleanup(args.name, args.confirm)
    except LabError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
