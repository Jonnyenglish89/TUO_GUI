# TUO GUI

A friendly Windows GUI for [tuo (Tyrant Unleashed Optimizer)](https://github.com/APN-Pucky/tyrant_optimize).


## Features

- Builds and runs tuo commands — hover any field for an explanation
- **Data menu** — downloads the latest game XMLs straight from the game server
- **Accounts** — paste your API data once (instructions built in), then one click downloads your owned cards and attack/defence decks; supports multiple accounts
- **Account sidebar** — pick accounts, choose attack or defence deck, and sim them all back to back, each with their own inventory
- **Results tab** — every result is parsed and saved (`tuo_gui_data\results.txt`), with a sortable table

## Getting started

1. Download `TUO_GUI_full_win64.zip` from [Releases](../../releases) and unzip it anywhere —
   it includes this project's build of `tuo.exe` (derived from the MIT-licensed
   [tyrant_optimize](https://github.com/APN-Pucky/tyrant_optimize), see `LICENSE-tuo.md`).
   Already have tuo? Grab just `TUO_GUI.exe` and drop it next to your `tuo.exe`.
2. Start TUO_GUI — on first run it offers to download the latest game data
   (rerun anytime via **Data → Update XMLs**).
3. *(Optional)* **Accounts → Add account** to link your game account — the dialog walks you
   through copying your API data from Firefox. Then **Accounts → Update owned cards & decks**.
4. Pick your decks/settings on the Sim tab and hit **Run Sim**.


## Privacy & security

- Your API credentials are stored as **plain text** in `tuo_gui_data\cookies` on your own
  computer and are only ever sent to the game's API server (`mobile.tyrantonline.com`).
- `.gitignore` excludes credentials, inventories, decks, settings, and results — nothing
  personal leaves your machine if you fork/contribute.
