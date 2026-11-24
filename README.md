# Debug Rust components in Spin

## What it is

A VS Code setup for debugging Rust HTTP components running in Spin. Set
breakpoints, step through your code, and inspect common variables,
including strings, vectors, URLs, and headers.

The reusable part is a single Python file, [spin_debug.py](.vscode/spin_debug.py).
This repository includes a small Rust app to try it with.

## How it works

Pressing **F5** builds the component with debug information, starts Spin with
`--debug-info`, and attaches CodeLLDB to Spin's HTTP worker.

## How to use it

1. Install Spin, Rust 1.94 or newer, and the **CodeLLDB** VS Code extension.
   Add the WebAssembly target:

   ```sh
   rustup target add wasm32-wasip2
   ```

2. Open this project in VS Code from a shell where `spin` and `cargo` are on PATH.
3. Set a breakpoint in the handler in [src/lib.rs](src/lib.rs), select
   **Debug Rust component in Spin**, and press **F5**.
4. Send a request from a terminal to hit the breakpoint:

   ```sh
   curl -i http://127.0.0.1:3107/
   ```

Expand `req` in the Variables panel to inspect the request, URL, and headers.
Spin output is saved to `.spin/debug.log`.

To use this setup in another project, copy [spin_debug.py](.vscode/spin_debug.py),
[launch.json](.vscode/launch.json), and [tasks.json](.vscode/tasks.json) into its
`.vscode` directory, merging the JSON configurations if they already exist.
Add a debug profile for your HTTP component in `spin.toml`:

```toml
[component.YOUR_COMPONENT.profile.debug]
source = "target/wasm32-wasip2/debug/YOUR_CRATE.wasm"
build.command = "cargo build --target wasm32-wasip2"
```

Replace `YOUR_COMPONENT` with the component ID and `YOUR_CRATE` with the Rust
crate name, using underscores in place of hyphens. Keep debug information in
`Cargo.toml`:

```toml
[profile.dev]
opt-level = 0
debug = 2
strip = "none"
```

Then follow the same F5 and HTTP request steps. VS Code loads the Python helper
through LLDB; no pip packages are needed. To change the port, edit `3107` in
`spin_debug.py` and use the new port in your requests.
