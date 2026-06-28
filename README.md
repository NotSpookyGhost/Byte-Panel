# Byte Panel

### *A Modern Docker Solution For Hosting Minecraft Servers*

---

## Overview

**Byte Panel** is a lightweight, responsive, dark-themed web management panel built specifically for self-hosting Minecraft server infrastructure on Unraid and Docker environments. Designed to eliminate the overhead of bulky, resource-heavy panels, Byte Panel gives administrators full lifecycle control over their server files, Java execution layers, and active processes right from a clean web interface. 

By leveraging native Docker isolation and integrating direct file management over local networks, it bridges the gap between raw container control and a streamlined gaming dashboard.

---

## Key Features

*   **Dynamic Engine Initialization:** Quickly build and deploy Vanilla, PaperMC, Forge, or Fabric server environments across a wide spectrum of legacy and modern Minecraft versions.
*   **Intelligent Dependency Injection:** Automatically detects the target Minecraft version and maps it to the precise native OpenJDK runtime (up to Java 25 via Alpine Edge) required to execute the server without manual configuration.
*   **Hardware Allocation Controls:** Features a dynamic memory allocation slider that automatically reads the host system's total available RAM limits to set execution thresholds safely.
*   **Robust Lifecycle Management:** Full background process management utilizing isolated subprocesses, complete with instant server initialization, graceful termination hooks, and automated log routing.
*   **Fail-Safe Backup & Reset Systems:** Integrated ZIP-archiving engines that timestamp and compress server files to a local backup directory, alongside a destructive "Purge & Reset" mechanism to cleanly wipe environments and start fresh.
*   **Security & Audit Readiness:** Built with structured logging to push authentication events and administrative interventions to local container standard output, ready to be ingested by external security monitors and Webhooks.

---

## Architecture & Integration

Byte Panel is structured with a Python-based backend running a high-frequency polling engine and a pure HTML5/JavaScript dashboard frontend. 

*   **File Syncing:** Maps server deployment directories directly to standard container data volumes, allowing instant drag-and-drop file configuration through local SMB shares or network file transfers.
*   **Process Isolation:** Runs the server.jar completely in the background, decoupling the web interface thread from the Minecraft execution loop to prevent page hangs or socket timeouts during heavy terrain generation.
*   **Data Portability:** Tracks environmental states, admin configuration matrices, and running PIDs via lightweight, externalized JSON layers for instant portability and persistent container migrations.

---

## Development & Roadmap

Byte Panel is actively developed with an architecture ready for deep network logging. Upcoming modules include a dedicated database-backed player history ledger, localized GeoIP resolution maps, and asynchronous query hooks to drive external automated security systems.