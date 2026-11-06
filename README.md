# KEMUS VPRO

> **The Declaration of War against the Silicon Valley Monoliths.**  
> Building is child’s play. “Developer” is an insult. “Systems Creator” is the future.  
> *We don’t just run code. We execute missions.*

**Kemus VPro** is a beta, local-first virtual desktop and modular workspace written as a Python standard-library web server with an embedded HTML/CSS/JavaScript interface. It brings a desktop shell, virtual filesystem, terminal, app catalog, settings, browser tools, and a YAML workflow studio into one browser window.

This is an experimental desktop-style application—not a Linux distribution, kernel, virtual machine, or security sandbox. Some catalog apps and system commands are simulations. The project is in **beta**; expect rough edges and verify behavior before relying on it.

## What’s in the repository

The main implementation and release artifacts are currently at the repository root:

| File | Description |
|---|---|
| `kemus.py` | Kemus 2.0 application: threaded HTTP server, embedded desktop UI, APIs, terminal, app catalog, settings, and workflow runner |
| `pyproject.toml` | Python package metadata and `kemus` command entry point |
| `install.sh` | Installer/launcher with environment detection and installation fallbacks |
| `kemus-2.0.tar.gz` | Kemus 2.0 source archive |
| `kemus-1.0(1).tar.gz` | Earlier source archive |
| `install(2).sh`, `kemus(2).py`, `pyproject(2).toml` | Duplicate-name uploads; compare with the canonical files before using them |

The runtime stores its virtual home, settings, installed-app list, workflow YAML files, and server log under `~/.kemus/`.

## Features

- **Desktop shell:** app launcher/search, desktop shortcuts, dock, task list, clock, and movable/resizable windows.
- **Virtual workspace:** file manager, text editor, starter files, and persistent virtual home under `~/.kemus/home/`.
- **Terminal:** built-in commands for file operations and text processing, pipelines, redirection, command chaining, environment variables, help/manual lookup, and selected network helpers. The implementation advertises 55+ commands; several system and package-manager commands return simulated output rather than inspecting or modifying the host.
- **App catalog:** categories and install/uninstall state for bundled app modules. This is an in-app catalog, not an operating-system package manager; many listed apps are placeholders or simulated.
- **Workflow Studio:** edit and save workflow YAML, then run its supported subset locally with step-by-step Server-Sent Events output. The syntax is GitHub Actions-inspired; workflows run through Kemus’s own command interpreter and **do not run on GitHub Actions runners**.
- **Customization:** eight themes, accent color, wallpaper, terminal font and size, plus saved settings.
- **Utilities:** calculator, notes, tasks, calendar, logs, process/system panels, manual pages, search/browser proxy, and other modules with varying levels of implementation.
- **Local HTTP API:** health and desktop metadata, virtual files, terminal, settings, catalog, search, manual pages, proxy, and workflow endpoints.

## Quick start

**Requirements:** Python 3.8 or newer and a modern browser.

Run directly from the repository root (no pip installation required):

```bash
python3 kemus.py
```

Open [http://127.0.0.1:3922](http://127.0.0.1:3922) in your browser. Stop the server with `Ctrl+C`.

Use a different port with:

```bash
KEMUS_PORT=4000 python3 kemus.py
```

To set a different bind address, the source also reads `KEMUS_HOST`. Keep it on loopback unless you have deliberately configured appropriate access controls; the application does not provide user authentication.

The installer can be launched with:

```bash
bash install.sh --help
bash install.sh
```

Review installer scripts before running them. The direct Python command is the simplest way to try the beta.

## Terminal and workflows

Type `help` in the terminal for the command list and `man COMMAND` for available manual text. Examples:

```text
ls | grep py | wc -l
echo hello > Documents/hello.txt
export PROJECT=kemus && echo $PROJECT
```

Workflow files are stored in `~/.kemus/workflows/`. Kemus seeds example workflows on first launch. Workflow YAML is parsed by a small project-specific parser and supports only a subset of common workflow syntax; it is not a general-purpose YAML implementation or a full GitHub Actions engine. Runs execute locally inside Kemus’s virtual filesystem command model, with output streamed to the UI.

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/health` | Health and version information |
| `GET` | `/api/desktop` | Desktop metadata |
| `GET` | `/api/fs?path=...` | List a virtual directory |
| `GET` | `/api/file?path=...` | Read a virtual file |
| `POST` | `/api/terminal` | Execute a Kemus terminal command |
| `POST` | `/api/file/save` | Save a virtual file |
| `GET` | `/api/workflows` | List stored workflows |
| `GET` | `/api/workflow/run?name=...` | Run a workflow and stream events |
| `POST` | `/api/workflow/save` | Save workflow text |
| `GET` | `/api/store`, `/api/installed` | Read app catalog and installed-app state |
| `POST` | `/api/store/install`, `/api/store/uninstall` | Change catalog installation state |
| `GET` / `POST` | `/api/settings`, `/api/settings/save` | Read or save UI settings |

## Security and beta warning

Kemus VPro is experimental and **not safe to expose as a public or shared service**. The server binds to loopback by default, but the bind host can be changed. There is no authentication or authorization layer in the shown implementation. Keep it local and use it only in an environment you control.

The virtual filesystem is application-level path handling, **not an OS security boundary**. In particular, Kemus’s Python command launches code in a subprocess as the same operating-system user running the server; setting its working directory to the virtual home does not sandbox it. Treat commands, workflow files, and code executed through Kemus as trusted code. Network helpers and the browser proxy make outbound requests; do not rely on their filtering as a complete security control.

Do not use this beta to store secrets or sensitive data. Report security issues privately through GitHub’s repository security reporting feature rather than publishing exploit details in a public issue.

## License and contributions

The project metadata declares MIT, but review the repository’s license files and included third-party assets before redistribution. Issues and pull requests are welcome; include reproduction steps and clearly distinguish implemented behavior from simulations or placeholders.

---

**Build systems. Keep them inspectable. Give creators the keys.**
